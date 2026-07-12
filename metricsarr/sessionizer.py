"""Metricsarr sessionizer -- the watch state machine. Pure, stdlib only.

Every rule here was bought with a review finding; the comments name them.
Spec S5.

Controller resolution (task-3): the brief's draft split "provisional close"
(_maybe_close) from "finalize" (_finalize_due) and gated finalization on
max(grace, merge_gap) measured from close_ts -- a 120s reopen window, and it
left a dead `if ...: return` in _maybe_close. This implementation instead
uses a single, coherent hold window: a session stays open and reopenable
until `now - close_ts >= hold`, where
    hold = max(client_gap_grace_s, 2 * effective_interval) + merge_gap_s
(= max(90, 30) + 120 = 210s at defaults). Accounting happens exactly once,
at finalization -- never at a provisional close -- so a merged session can
never be double-counted.

One further mechanic, derived empirically from the test contract (the plain
prose "the gap credits nothing, resume from now" undercredits a session that
survives several on-cadence zero polls before reopening -- see
test_grace_survives_a_player_retry_gap wanting 180s from a 60s+120s+120s
shape, not the 165s a blind "reset to now" gives):

Per-poll crediting is computed from `last_seen_ts`, the timestamp of the
*immediately preceding poll for this uuid, whether or not it saw clients*
(not from the last *credited* poll). A zero-client poll still advances
`last_seen_ts`, so a reopen that lands back on the normal polling cadence
(as in a grace-window retry) is credited like any other in-cadence tick.
`close_ts` is pinned to `last_seen_ts` (the last poll observed, positive or
not) the moment a zero-client poll is seen -- since `last_seen_ts` has not
yet advanced to the *current* poll at that point, this is exactly the last
poll that saw clients >= 1.

Every delta is credited the same way regardless of context: `min(delta, cap)`,
clamped to zero if negative (backward clock step). Fixed 2026-07-12: an
earlier revision special-cased "a zero-client tick was observed since the
last credit" to credit nothing beyond the cap, on the theory that such a gap
is "proven" dead time. That distinction was a magnitude cliff, not a real
proven/unproven split (a delta of exactly `cap` credited the cap; one tick
over credited zero), and it erred in the dangerous under-counting direction.
Uniform capping is the safe-by-design choice: it can only ever over-count,
never zero out real watch time (test_merged_session_counts_once_not_twice:
the 60s untracked silence between the zero poll and the reopen now credits
its capped floor of 30s, giving 150 + 30 + 300 = 480, the documented safe
over-count bound).

The min_watch_seconds qualifying gate is evaluated against the session's
`accumulated` credit -- the same clamped value that gets recorded as
`watch_seconds` -- not against raw wall-clock span. Gating on raw span was
the Critical defect this module shipped with: it let a backward NTP step
(one poll where the clock steps back) shrink `close_ts - opened_ts` below
the threshold and forfeit an already-earned watch, and it let five separate
sub-threshold surfs merge (via the hold/reopen window) into a single session
whose *span* alone crossed 120s while its *credited* time never did. Gating
on `accumulated` makes "watch qualifies" and "watch_seconds" the same
number, so a recorded watch can never read as internally incoherent (a
"watch" below the watch threshold).
"""
from __future__ import annotations

import time

DEFAULTS = {"min_watch_seconds": 120.0, "client_gap_grace_s": 90.0,
            "merge_gap_s": 120.0, "poll_interval_s": 15.0}


def bucket_key(ts):
    """UTC hour bucket. Bucket keys are UTC; the scheduler is local. This is the
    only UTC arithmetic in the plugin -- consumers convert explicitly."""
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


class _Session:
    __slots__ = ("accumulated", "last_seen_ts", "close_ts")

    def __init__(self, now):
        self.accumulated = 0.0
        # Timestamp of the immediately preceding poll for this uuid, positive
        # or zero -- drives the per-poll credit delta (consistency #15: a
        # channel skipped by a poll must neither lose nor double-credit time).
        self.last_seen_ts = now
        # None while the last poll saw clients >= 1. Pinned to the current
        # last_seen_ts (i.e. the last poll that saw clients >= 1, since
        # last_seen_ts has not yet advanced when this fires) the moment a
        # zero-client poll is observed, and held fixed (not re-pinned) until
        # the session reopens or finalizes. Not the moment the hold window
        # expires -- the zero-client tail credits no watch time, so it must
        # not extend last_watched either (consistency #7).
        self.close_ts = None


def _blank_record(now):
    return {"watch_count": 0, "watch_seconds": 0.0, "tune_count": 0,
            "last_watched": None, "last_tuned": None, "first_seen": now}


class Sessionizer:
    def __init__(self, thresholds=None, channels=None):
        merged = dict(DEFAULTS)
        merged.update(thresholds or {})
        self.t = merged
        self.channels = dict(channels or {})
        self.open_sessions = {}
        self.coverage = {}
        self._last_tick_ts = None

    # ---- coverage -----------------------------------------------------------
    def _mark_coverage(self, now):
        bucket = self.coverage.setdefault(bucket_key(now),
                                          {"ticks": 0, "max_gap_s": 0.0})
        bucket["ticks"] += 1
        if self._last_tick_ts is not None:
            gap = now - self._last_tick_ts
            if gap > bucket["max_gap_s"]:
                bucket["max_gap_s"] = gap
        self._last_tick_ts = now

    def prune_coverage(self, now, keep_days=45):
        cutoff = bucket_key(now - keep_days * 86400)
        for key in [k for k in self.coverage if k < cutoff]:
            del self.coverage[key]

    # ---- session machine ----------------------------------------------------
    def reset_sessions(self):
        """Leader change / restart: drop open sessions rather than adopt them.

        Losing an in-flight session is a rounding error against a 30-day question;
        adoption cost a checkpoint format and four tests for no user benefit
        (product #1).
        """
        self.open_sessions = {}
        self._last_tick_ts = None

    def observe(self, now, present, effective_interval=None):
        """One poll. `present` maps uuid -> client count for every channel whose
        metadata key exists. A uuid absent from `present` has no metadata key.

        Known limitation: the hold window (see below) is only evaluated when a
        poll arrives, so a total collector stall (no polls at all, e.g. the
        process is down) never closes an open session -- two genuinely separate
        viewings separated by a long stall merge into one. Not dangerous: it
        slightly under-counts `watch_count` while `watch_seconds` is roughly
        preserved (still credited per-poll, capped as usual), and the coverage
        buckets exist precisely to let downstream discount stalled hours.
        """
        interval = float(effective_interval or self.t["poll_interval_s"])
        self._mark_coverage(now)

        # The hold window must span at least two polls even when over-budget
        # throttling doubles the interval to 120s -- a bare 90s grace would
        # close on the FIRST zero-client poll, exactly when it is most needed
        # (consistency #6). merge_gap_s is then added on top so a reopen
        # inside that combined window continues the same session
        # (consistency #7).
        hold = max(float(self.t["client_gap_grace_s"]), 2.0 * interval) \
            + float(self.t["merge_gap_s"])
        cap = 2.0 * interval          # max credit per poll: clamps clock jumps

        for uuid, clients in present.items():
            if clients >= 1:
                self._credit(uuid, now, cap)

        for uuid in list(self.open_sessions):
            if present.get(uuid, 0) >= 1:
                continue
            self._advance_or_finalize(uuid, now, hold)

    def _credit(self, uuid, now, cap):
        record = self.channels.setdefault(uuid, _blank_record(now))
        session = self.open_sessions.get(uuid)

        if session is None:
            record["tune_count"] += 1
            record["last_tuned"] = now
            self.open_sessions[uuid] = _Session(now)
            return

        delta = now - session.last_seen_ts
        if delta < 0:                 # backward clock step
            delta = 0.0

        # Uniform cap regardless of whether a zero-client tick was seen since
        # the last credit (module docstring): this can only over-count, never
        # forfeit real watch time.
        session.accumulated += min(delta, cap)
        session.close_ts = None

        session.last_seen_ts = now

    def _advance_or_finalize(self, uuid, now, hold):
        """A poll with no clients for this uuid: pin close_ts to the last poll
        that saw clients (once), advance the any-poll anchor so a subsequent
        reopen's delta is measured from here (not from the original close),
        then finalize once the hold window elapses with no reopen. Accounting
        happens exactly once, here at finalization -- never at the moment
        close_ts is pinned -- so a merged session can never be double-counted
        (consistency #7)."""
        session = self.open_sessions[uuid]
        if session.close_ts is None:
            # Pin close_ts to the last poll that saw clients >= 1 -- i.e. the
            # current last_seen_ts -- before advancing last_seen_ts below.
            session.close_ts = session.last_seen_ts
        session.last_seen_ts = now
        if now - session.close_ts < hold:
            return
        self._finalize(uuid, session)

    def _finalize(self, uuid, session):
        del self.open_sessions[uuid]
        record = self.channels[uuid]
        # Qualifying gate uses the clamped `accumulated` credit -- the same
        # value recorded as watch_seconds -- not raw wall-clock span (module
        # docstring, final paragraph).
        if session.accumulated >= float(self.t["min_watch_seconds"]):
            record["watch_count"] += 1
            record["watch_seconds"] += session.accumulated
            record["last_watched"] = session.close_ts
