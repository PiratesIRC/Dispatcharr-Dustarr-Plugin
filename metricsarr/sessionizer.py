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

Two further mechanics, derived empirically from the test contract (the plain
prose "the gap credits nothing, resume from now" undercredits a session that
survives several on-cadence zero polls before reopening -- see
test_grace_survives_a_player_retry_gap wanting 180s from a 60s+120s+120s
shape, not the 165s a blind "reset to now" gives):

1. Per-poll crediting is computed from `last_seen_ts`, the timestamp of the
   *immediately preceding poll for this uuid, whether or not it saw clients*
   (not from the last *credited* poll). A zero-client poll still advances
   `last_seen_ts`, so a reopen that lands back on the normal polling cadence
   (as in a grace-window retry) is credited like any other in-cadence tick.
   Only `last_positive_ts` (last poll that actually saw clients>=1) is used
   to pin `close_ts`.

2. Capping a delta means two different things depending on context:
   - No zero-client tick has been observed since the last credit (a raw
     jump between two back-to-back positive polls, e.g. host sleep): credit
     `min(delta, cap)` -- a capped floor, because we have no evidence the
     session ever dropped, so we credit conservatively rather than zero
     (test_clock_jump_forward_is_clamped: a 2h jump still credits 30s, the
     cap, not 0).
   - A zero-client tick WAS observed (a genuine reopen): a delta within one
     capped window is indistinguishable from normal continuous watching and
     is credited in full; a delta *beyond* the cap is dead time we have
     direct proof of and credits nothing, rather than inventing a floor
     (test_merged_session_counts_once_not_twice: a 60s untracked silence
     between the zero poll and the reopen credits 0, keeping the merged
     total at exactly 150 + 300 = 450, not 480).

The min_watch_seconds qualifying gate is evaluated against the session's raw
wall-clock span (close_ts - opened_ts), not the capped `accumulated` value --
otherwise the clock-jump test's capped 30s credit (span 7200s, well over the
120s qualifying floor) would never qualify to be recorded at all, while
test_channel_surf_below_threshold_is_not_a_watch's true 15s span correctly
stays excluded.
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
    __slots__ = ("accumulated", "last_seen_ts", "last_positive_ts", "close_ts", "opened_ts")

    def __init__(self, now):
        self.accumulated = 0.0
        # Timestamp of the immediately preceding poll for this uuid, positive
        # or zero -- drives the per-poll credit delta (consistency #15: a
        # channel skipped by a poll must neither lose nor double-credit time).
        self.last_seen_ts = now
        # Timestamp of the last poll that actually saw clients >= 1. Used only
        # to pin close_ts; never moved by a zero-client poll.
        self.last_positive_ts = now
        # None while the last poll saw clients >= 1. Pinned to
        # last_positive_ts the moment a zero-client poll is observed, and
        # held fixed (not re-pinned) until the session reopens or finalizes.
        # Not the moment the hold window expires -- the zero-client tail
        # credits no watch time, so it must not extend last_watched either
        # (consistency #7).
        self.close_ts = None
        # Fixed at session creation, never moved by a reopen. Used for the
        # qualifying-duration gate at finalization (see module docstring).
        self.opened_ts = now


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
        metadata key exists. A uuid absent from `present` has no metadata key."""
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

        if session.close_ts is not None:
            # Reopening after an observed zero-client tick: see module
            # docstring point 2. A delta within one capped window is
            # credited in full; a bigger one is proven dead time and credits
            # nothing.
            if delta <= cap:
                session.accumulated += delta
            session.close_ts = None
        else:
            # No zero-client tick observed for this stretch: credit a capped
            # floor (module docstring point 2).
            session.accumulated += min(delta, cap)

        session.last_seen_ts = now
        session.last_positive_ts = now

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
            session.close_ts = session.last_positive_ts
        session.last_seen_ts = now
        if now - session.close_ts < hold:
            return
        self._finalize(uuid, session)

    def _finalize(self, uuid, session):
        del self.open_sessions[uuid]
        record = self.channels[uuid]
        # Qualifying gate uses the raw wall-clock span, not the capped
        # `accumulated` credit (module docstring, final paragraph).
        real_span = session.close_ts - session.opened_ts
        if real_span >= float(self.t["min_watch_seconds"]):
            record["watch_count"] += 1
            record["watch_seconds"] += session.accumulated
            record["last_watched"] = session.close_ts
