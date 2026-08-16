"""Dustarr storage — usage.json under /data/dustarr. Stdlib only.

Leader-only writer (enforced by the collector, not here). A corrupt file is
SIDELINED, never blanked: an empty usage.json read as "nobody watched anything"
is the plugin's mass-casualty failure mode (spec S6, S11.4).
"""
from __future__ import annotations

import json
import os

USAGE = "usage.json"
CIRCUIT_FAILS = 3
CIRCUIT_RETRY_S = 300.0


class Storage:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stats = {"dropped_writes": 0, "corrupt_sidelines": 0}
        self._fail_streak = 0
        self._retry_after = 0.0

    def _path(self, name=USAGE):
        return os.path.join(self.data_dir, name)

    def _circuit_open(self, now):
        return self._fail_streak >= CIRCUIT_FAILS and now < self._retry_after

    def _io_fail(self, now):
        self.stats["dropped_writes"] += 1
        self._fail_streak += 1
        if self._fail_streak >= CIRCUIT_FAILS:
            self._retry_after = now + CIRCUIT_RETRY_S

    def write(self, payload, now):
        if self._circuit_open(now):
            self.stats["dropped_writes"] += 1
            return False
        tmp = self._path(USAGE + ".tmp")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            body = dict(payload)
            body["written_at"] = now
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(body, fh, separators=(",", ":"), default=str)
                # Force the bytes to disk BEFORE the rename: without this, a
                # power loss inside the filesystem journal window can publish
                # a truncated file, which load() then sidelines -- silently
                # restarting the irreplaceable dataset. One fsync per flush
                # (every 60 s) is the whole cost.
                fh.flush()
                os.fsync(fh.fileno())
            # Same-directory rename: cross-device os.replace fails.
            os.replace(tmp, self._path())
            self._fail_streak = 0
            return True
        except OSError:
            # Best-effort cleanup: never let a stale .tmp linger after a
            # failed replace. Swallow errors here too — cleanup must not
            # mask the original failure or raise of its own accord.
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._io_fail(now)
            return False

    def load(self, now):
        path = self._path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("usage.json is not an object")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            try:
                os.replace(path, self._sideline_path(path, now))
                # Only count a sideline that actually happened.
                self.stats["corrupt_sidelines"] += 1
            except OSError:
                pass
            return {}

    @staticmethod
    def _sideline_path(path, now):
        """Pick a sideline path that never overwrites an existing one.

        One-second granularity means two corruptions in the same second
        would otherwise collide; the forensic bytes of the earlier
        corruption must never be silently clobbered.
        """
        base = f"{path}.corrupt-{int(now)}"
        candidate = base
        suffix = 0
        while os.path.exists(candidate):
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate

    def ensure_stats_since(self, payload, now):
        """Stamp stats_since exactly once per usage.json lifetime.

        Never carried across a corrupt-sideline: a recreated file gets a fresh
        stats_since, which re-arms the coverage/age gates by construction.
        """
        out = dict(payload or {})
        meta = dict(out.get("meta") or {})
        if not meta.get("stats_since"):
            meta["stats_since"] = now
        out["meta"] = meta
        out.setdefault("channels", {})
        return out
