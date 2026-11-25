"""Dustarr storage: usage.json in Dustarr's data directory. Stdlib only.

Leader-only writer (enforced by the collector, not here). A corrupt file is
SIDELINED, never blanked: an empty usage.json read as "nobody watched anything"
is the plugin's mass-casualty failure mode (spec S6, S11.4).

legacy_path is set only during the data-directory rollout (see load). It names
the usage.json that a previous release still writes under /data/dustarr.
"""
from __future__ import annotations

import json
import os

USAGE = "usage.json"
CIRCUIT_FAILS = 3
CIRCUIT_RETRY_S = 300.0


def _object_bytes(data):
    """The parsed JSON object in data, or None when data is not one. Never raises."""
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_file(path):
    """The bytes of path, or None when it is missing or unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def adopt_newer_file(new_path, legacy_path, now):
    """Copy legacy_path over new_path when the old file is the newer state. Rollout only.

    Returns "adopted" when the legacy file replaced a missing new file or is newer by mtime,
    "adopted-over-corrupt" when it replaced an unparseable new file (which is renamed aside
    first, never deleted), and None when nothing changed. A legacy file that does not parse as
    a JSON object is never copied, so a corrupt old file cannot replace a good new one.
    Raises OSError on a failed write; the caller decides whether that matters.
    """
    if not legacy_path:
        return None
    try:
        legacy_mtime = os.path.getmtime(legacy_path)
    except OSError:
        return None                       # no old file, so nothing to adopt
    exists = os.path.exists(new_path)
    current = _read_file(new_path) if exists else None
    if exists and current is None:
        return None                       # present but unreadable: never overwrite what we cannot read
    if current is not None and _object_bytes(current) is not None \
            and legacy_mtime <= os.path.getmtime(new_path):
        return None                       # the new file is valid and at least as new: keep it
    legacy = _read_file(legacy_path)
    if legacy is None or _object_bytes(legacy) is None:
        return None
    if current is None:
        reason = "adopted"
    elif _object_bytes(current) is None:
        reason = "adopted-over-corrupt"
        os.replace(new_path, Storage._sideline_path(new_path, now))
    else:
        reason = "adopted"
    tmp = new_path + ".adopt.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(legacy)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, new_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return reason


class Storage:
    def __init__(self, data_dir, legacy_path=None):
        self.data_dir = data_dir
        self.legacy_path = legacy_path
        self.stats = {"dropped_writes": 0, "corrupt_sidelines": 0,
                      "legacy_adoptions": 0, "legacy_adopt_failures": 0}
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

    def adopt_legacy(self, now):
        """Rollout: adopt the old usage.json when it is the newer state. Never raises.

        A new-code leader reaches this through load(), on every lease acquisition, so an
        old-code leader's writes since the carry-over are not lost when the lease changes hands.
        """
        if not self.legacy_path:
            return None
        try:
            reason = adopt_newer_file(self._path(), self.legacy_path, now)
        except OSError:
            self.stats["legacy_adopt_failures"] += 1
            return None
        if reason:
            self.stats["legacy_adoptions"] += 1
        return reason

    def load(self, now):
        self.adopt_legacy(now)
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
