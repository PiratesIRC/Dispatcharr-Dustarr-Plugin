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
    """Compare-and-renew leader lease. Never steals a foreign lease."""

    def __init__(self, redis, token, key=LEADER_KEY, ttl=LEASE_TTL):
        self.r = redis
        self.token = token
        self.key = key
        self.ttl = ttl
        self.owned = False

    def tick(self, now):
        try:
            current = to_text(self.r.get(self.key))
            if current == self.token:
                self.r.expire(self.key, self.ttl)
                self.owned = True
            elif current is None:
                self.owned = bool(self.r.set(self.key, self.token, nx=True,
                                             ex=self.ttl))
            else:
                self.owned = False
        except Exception:
            self.owned = False
        return self.owned

    def release(self):
        try:
            if to_text(self.r.get(self.key)) == self.token:
                self.r.delete(self.key)
        except Exception:
            pass
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
        self.stats = {"cycle_seq": 0, "channels_seen": 0, "redis_errors": 0,
                      "over_budget_count": 0, "total_ticks": 0,
                      "last_tick_ts": None, "last_error": None}

    # ---- helpers ------------------------------------------------------------
    def base_tick(self):
        return self.effective_interval

    def _cycle_ms(self, start):
        return (time.monotonic() - start) * 1000.0

    def _note_error(self, kind):
        self.stats[kind] += 1
        self.stats["last_error"] = traceback.format_exc(limit=8)[-800:]

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
        for key in self.r.scan_iter(match=SCAN_PATTERN, count=SCAN_COUNT):
            uuid = uuid_from_key(key)
            if uuid:
                uuids.append(uuid)

        present = {}
        if uuids:
            pipe = self.r.pipeline()
            for uuid in uuids:
                pipe.scard(f"live:channel:{uuid}:clients")
            counts = pipe.execute()
            for uuid, count in zip(uuids, counts, strict=True):
                present[uuid] = int(count or 0)

        self.stats["channels_seen"] = len(present)
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
        # Re-verify the lease immediately before writing: a deposed leader must
        # never write (spec S11.6).
        if not self.lease.tick(now):
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
        if not is_leader:
            if self._was_leader:
                # Deposed: forfeit in-flight sessions rather than adopt them later.
                self.sessionizer.reset_sessions()
            self._was_leader = False
            return
        if not self._was_leader:
            # Freshly elected: never inherit another leader's in-flight sessions,
            # and re-read usage.json -- an interim leader (or this worker's own
            # prior term) may have written since we last held the lease.
            self.sessionizer.reset_sessions()
            self._acquire_state(now)
        self._was_leader = True

        self.stats["cycle_seq"] += 1
        self.stats["total_ticks"] += 1
        self.stats["last_tick_ts"] = now

        try:
            present = self._sample(now)
        except Exception:
            self._note_error("redis_errors")
            return

        self.sessionizer.observe(now, present, self.effective_interval)

        self._throttle(self._cycle_ms(start))

        if now - self._last_flush >= FLUSH_INTERVAL_S or self._last_flush == 0.0:
            self._flush(now)

    def shutdown(self):
        try:
            self._flush(self.wall())
        except Exception:
            pass
        self.lease.release()
