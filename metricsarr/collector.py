"""Metricsarr collector -- leader lease + tick loop. Stdlib only, Redis only.

NEVER touches the ORM or Postgres (spec S11.2). The redis client is injected and
duck-typed: get/set/delete/expire/scard/scan_iter/pipeline.
"""
from __future__ import annotations

import time
import traceback

LEADER_KEY = "metricsarr:leader"
LEASE_TTL = 60

SCAN_PATTERN = "live:channel:*:metadata"
SCAN_COUNT = 1000

SOFT_BUDGET_MS = 50.0
HARD_BUDGET_MS = 250.0
THROTTLE_CEILING_S = 120.0
OVER_BUDGET_TRIP = 3
UNTHROTTLE_AFTER = 10
FLUSH_INTERVAL_S = 60.0
COVERAGE_KEEP_DAYS = 45


def to_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def uuid_from_key(key):
    """live:channel:<uuid>:metadata -> <uuid>. The segment is the channel UUID,
    never the numeric id (verified in redis_keys.py + the proxy routes)."""
    parts = to_text(key).split(":")
    return parts[2] if len(parts) >= 4 else None


class Lease:
    """Compare-and-renew leader lease. Never steals a foreign lease.

    `tick()` may both renew a key we already own AND acquire a FREE one (via
    SET NX) -- that SET NX branch is a genuine leadership *acquisition* and is
    flagged via `acquired_fresh` so callers can detect it even when their own
    local `_was_leader` belief never observed the loss (task review FIX2).

    `renew()` is the verify-WITHOUT-acquiring half: it renews a key we already
    own but must NEVER SET NX a free key. Callers that need to fence a write
    against "am I still actually allowed to write" (FIX1) must use `renew()`,
    never `tick()` -- `tick()` would happily "become leader" just in time to
    take over a free key and write, which is exactly the bug a write fence
    exists to prevent.
    """

    def __init__(self, redis, token, key=LEADER_KEY, ttl=LEASE_TTL):
        self.r = redis
        self.token = token
        self.key = key
        self.ttl = ttl
        self.owned = False
        # True only for the tick() call that just SET-NX'd a previously-free
        # key -- a real leadership transition, distinct from renewing a key
        # we already held. Reset at the top of every tick() call.
        self.acquired_fresh = False
        # Set only from inside an except block (so it carries a real
        # traceback), and left set until a caller drains it. Lets callers
        # attribute a Redis-path failure without needing their own active
        # exception handler (FIX4).
        self.last_error = None

    def tick(self, now):
        self.acquired_fresh = False
        self.last_error = None
        try:
            current = to_text(self.r.get(self.key))
            if current == self.token:
                self.r.expire(self.key, self.ttl)
                self.owned = True
            elif current is None:
                acquired = bool(self.r.set(self.key, self.token, nx=True,
                                           ex=self.ttl))
                self.owned = acquired
                self.acquired_fresh = acquired
            else:
                self.owned = False
        except Exception:
            # A transient Redis blip must not read as deposition (FIX4):
            # deposition should require an OBSERVED foreign token, not an
            # I/O failure -- otherwise a one-off GET error silently forfeits
            # every in-flight session, which is the dangerous direction.
            # Leave `owned` at its last confirmed value; only record the
            # error.
            self.last_error = traceback.format_exc(limit=8)[-800:]
        return self.owned

    def renew(self, now):
        """Verify-and-renew WITHOUT acquiring: renews only if we already own
        the key; NEVER SET NX a free key (FIX1). Used to fence writes.

        Fails CLOSED on a Redis error -- the opposite of tick()'s fail-open
        (C1). tick()'s fail-open answers "should an in-flight session be
        forfeit on a blip" (no -- that needs an OBSERVED foreign token).
        renew() answers a different question: "is it SAFE TO WRITE right
        now", asked immediately before every flush. A worker whose OWN Redis
        connection wedges (broken socket in this worker, Redis itself and
        the lease fine for everyone else) must not verify a write against a
        cached `owned` from its last successful call -- that is not a
        verification, it is a stale belief, and it is exactly how a
        deposed-but-wedged worker keeps writing forever alongside the real
        new leader (dual-writer clobber). So on exception: return False
        (never write what you cannot verify) and leave `self.owned`
        untouched -- callers must gate writes on renew()'s RETURN VALUE,
        never on `.owned` after calling it.
        """
        self.last_error = None
        try:
            if to_text(self.r.get(self.key)) == self.token:
                self.r.expire(self.key, self.ttl)
                self.owned = True
            else:
                self.owned = False
        except Exception:
            self.last_error = traceback.format_exc(limit=8)[-800:]
            return False
        return self.owned

    def release(self):
        self.last_error = None
        try:
            if to_text(self.r.get(self.key)) == self.token:
                self.r.delete(self.key)
        except Exception:
            self.last_error = traceback.format_exc(limit=8)[-800:]
        self.owned = False


class Collector:
    def __init__(self, redis, sessionizer, storage, thresholds, token,
                 wall=time.time):
        self.r = redis
        self.sessionizer = sessionizer
        self.storage = storage
        self.wall = wall
        self.token = token
        self.lease = Lease(redis, token)
        self.configured_interval = float(thresholds.get("poll_interval_s", 15.0))
        self.effective_interval = self.configured_interval
        self._over_budget_streak = 0
        self._in_budget_streak = 0
        self._was_leader = False
        self._stats_since = None
        self._last_flush = 0.0
        self._last_sample = 0.0
        self.stats = {"cycle_seq": 0, "channels_seen": 0, "redis_errors": 0,
                      "over_budget_count": 0, "total_ticks": 0,
                      "last_tick_ts": None, "last_error": None,
                      "malformed_keys": 0}

    # ---- helpers ------------------------------------------------------------
    def base_tick(self):
        """How often the (external) thread loop should call run_tick().

        Must stay <= LEASE_TTL/3 even while throttled to THROTTLE_CEILING_S
        (120s > LEASE_TTL=60s), or a throttled leader's lease lapses every
        cycle and leadership flaps, forfeiting in-flight sessions (FIX3).
        The *sampling* cadence (how often we actually SCAN + observe) stays
        `effective_interval` regardless -- see the gate in run_tick -- so
        throttling still sheds load; only the renewal cadence speeds up.
        """
        return min(self.effective_interval, LEASE_TTL / 3.0)

    def _cycle_ms(self, start):
        return (time.monotonic() - start) * 1000.0

    def _note_error(self, kind):
        self.stats[kind] += 1
        self.stats["last_error"] = traceback.format_exc(limit=8)[-800:]

    def _drain_lease_error(self):
        """Surface a lease-path Redis error into stats without raising
        (FIX4). Never overwrites stats with a stale/absent error."""
        if self.lease.last_error:
            self.stats["redis_errors"] += 1
            self.stats["last_error"] = self.lease.last_error
            self.lease.last_error = None

    def _acquire_state(self, now):
        """Chain load() -> ensure_stats_since() -> (caller writes). Run once on
        EVERY leadership acquisition (not just once ever): a worker that loses
        and later regains the lease must pick up whatever an interim leader
        wrote, and a usage.json that was corrupt-sidelined by that load() must
        hand back a fresh stats_since -- never one this worker cached from a
        previous, now-stale, in-memory copy (task-2 review guarantee)."""
        data = self.storage.ensure_stats_since(self.storage.load(now), now)
        self.sessionizer.channels = dict(data.get("channels") or {})
        self.sessionizer.coverage = dict((data.get("meta") or {}).get("coverage") or {})
        self._stats_since = (data.get("meta") or {}).get("stats_since") or now

    def _sample(self, now):
        """Full SCAN for the presence set, then a pipelined SCARD on EVERY present
        channel. No round-robin cap: a cap can miss an entire 2-minute watch."""
        uuids = []
        malformed = 0
        for key in self.r.scan_iter(match=SCAN_PATTERN, count=SCAN_COUNT):
            uuid = uuid_from_key(key)
            if uuid:
                uuids.append(uuid)
            else:
                malformed += 1

        present = {}
        if uuids:
            pipe = self.r.pipeline()
            for uuid in uuids:
                pipe.scard(f"live:channel:{uuid}:clients")
            counts = pipe.execute()
            for uuid, count in zip(uuids, counts, strict=True):
                present[uuid] = int(count or 0)

        self.stats["channels_seen"] = len(present)
        self.stats["malformed_keys"] += malformed
        return present

    def _throttle(self, cycle_ms):
        if cycle_ms > HARD_BUDGET_MS:
            self.stats["over_budget_count"] += 1
            self._over_budget_streak += 1
            self._in_budget_streak = 0
            if self._over_budget_streak >= OVER_BUDGET_TRIP:
                self.effective_interval = min(self.effective_interval * 2.0,
                                              THROTTLE_CEILING_S)
                self._over_budget_streak = 0
        elif cycle_ms <= SOFT_BUDGET_MS:
            self._over_budget_streak = 0
            self._in_budget_streak += 1
            if self._in_budget_streak >= UNTHROTTLE_AFTER:
                self.effective_interval = self.configured_interval
                self._in_budget_streak = 0

    def _flush(self, now):
        # Belt and braces: never write never-loaded state. A worker that
        # never acquired leadership (and so never ran _acquire_state) must
        # never manufacture an empty usage.json (FIX1).
        if self._stats_since is None:
            return
        # Re-verify the lease immediately before writing, WITHOUT acquiring a
        # free key: .tick() would SET NX a free key and let a follower "take"
        # the lease just in time to write its never-loaded empty state
        # (spec S11.6 / FIX1). .renew() only ever renews a key we already own.
        renewed = self.lease.renew(now)
        self._drain_lease_error()
        if not renewed:
            return
        self.sessionizer.prune_coverage(now, keep_days=COVERAGE_KEEP_DAYS)
        payload = {
            "channels": self.sessionizer.channels,
            "meta": {
                "stats_since": self._stats_since,
                "coverage": self.sessionizer.coverage,
                "self_health": {
                    "total_ticks": self.stats["total_ticks"],
                    "last_tick_ts": self.stats["last_tick_ts"],
                    "effective_interval": self.effective_interval,
                    "redis_errors": self.stats["redis_errors"],
                    "over_budget_count": self.stats["over_budget_count"],
                    "last_error": self.stats["last_error"],
                    "malformed_keys": self.stats["malformed_keys"],
                    "dropped_writes": self.storage.stats["dropped_writes"],
                    "corrupt_sidelines": self.storage.stats["corrupt_sidelines"],
                },
            },
        }
        if self.storage.write(payload, now):
            self._last_flush = now

    # ---- the tick -----------------------------------------------------------
    def run_tick(self):
        start = time.monotonic()
        now = self.wall()

        is_leader = self.lease.tick(now)
        self._drain_lease_error()
        if not is_leader:
            if self._was_leader:
                # Deposed: forfeit in-flight sessions rather than adopt them later.
                self.sessionizer.reset_sessions()
            self._was_leader = False
            return
        # A leadership TRANSITION is either "I did not believe I was leader a
        # moment ago" OR "I just acquired a FREE key via SET NX"
        # (`acquired_fresh`) -- the latter catches an UNOBSERVED loss: a
        # worker stalled past LEASE_TTL never ran a tick to see a foreign
        # token, so its `_was_leader` never flipped, yet an interim leader
        # may have come and gone in the gap and written state this worker
        # never saw (task review FIX2). Keying off `_was_leader` alone missed
        # exactly this case and caused a lost update.
        if not self._was_leader or self.lease.acquired_fresh:
            self.sessionizer.reset_sessions()
            self._acquire_state(now)
        self._was_leader = True

        # Sampling cadence gate (FIX3): run_tick may now be called more often
        # than effective_interval -- base_tick() returns
        # min(effective_interval, LEASE_TTL/3) precisely so the lease above
        # gets renewed often enough even while throttled to
        # THROTTLE_CEILING_S=120s > LEASE_TTL=60s. Only the renewal above must
        # happen on every call; the actual SCAN/observe must stay on the
        # (possibly throttled) effective_interval cadence, or throttling
        # would silently stop shedding load.
        if self._last_sample != 0.0 and now - self._last_sample < self.effective_interval:
            return
        self._last_sample = now

        self.stats["cycle_seq"] += 1
        self.stats["total_ticks"] += 1
        self.stats["last_tick_ts"] = now

        try:
            present = self._sample(now)
        except Exception:
            # Don't leave a stale previous count sitting in stats behind a
            # failed sample -- it must not look like a healthy, unchanged
            # channel count (FIX6).
            self.stats["channels_seen"] = 0
            self._note_error("redis_errors")
            return

        self.sessionizer.observe(now, present, self.effective_interval)

        self._throttle(self._cycle_ms(start))

        if now - self._last_flush >= FLUSH_INTERVAL_S or self._last_flush == 0.0:
            self._flush(now)

    def shutdown(self):
        try:
            # Only a worker that actually held the lease at some point has
            # anything of its own to flush. A follower flushing here is
            # exactly the FIX1 bug: it has never loaded state, so it would
            # write its blank in-memory `channels` over a real usage.json.
            if self._was_leader:
                self._flush(self.wall())
        except Exception:
            pass
        self.lease.release()
        self._drain_lease_error()
