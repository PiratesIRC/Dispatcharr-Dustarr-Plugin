"""Metricsarr reports -- join the ORM channel universe against usage.json.

THE INVARIANT (spec S6): the channel universe is the ORM. usage.json is a SPARSE
OVERLAY of watched channels only. A channel absent from it is NEVER-WATCHED --
the default, expected state, not an error. That is the plugin's entire purpose.

`usage.json` is UNTRUSTED FILE INPUT: storage.load() validates only that the top
level is a dict, never per-record field types. `_sanitize_usage()` coerces every
per-channel field once, at the top of build_model, so neither this module's own
reads nor gates.evaluate() (which is not None/str-tolerant) can be crashed by a
malformed record.

`too_new` is a PEER of `never_watched` in `counts` and in the gate/webhook never-
watched number, NOT a sub-count of it -- a fresh M3U import must not inflate the
one actionable headline number or spuriously trip the >60% ceiling gate. Its
entries still live inside the `never_watched` LIST (with reason="too_new") so
Task 9 can render them there. `counts` sums to `total_channels`:
    never_watched + too_new + tuned_never_qualified + watched + excluded
    + unobservable == total_channels
"""
from __future__ import annotations

import time

try:
    from . import gates, gateway
except ImportError:                     # standalone (non-package) import path
    import gates
    import gateway

EMPTY = {"watch_count": 0, "watch_seconds": 0.0, "tune_count": 0,
         "last_watched": None, "last_tuned": None, "first_seen": None}


def _coerce_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_timestamp(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_usage(usage):
    """Coerce untrusted usage.json record fields (I4).

    storage.load() only validates that the top level is a dict; per-record
    values (watch_count, watch_seconds, timestamps, ...) are never coerced and
    can arrive as None, strings, or otherwise malformed. Every downstream
    reader in this module -- and gates.evaluate(), which is not None/str-
    tolerant -- assumes numeric fields are numeric. Coerce once, here, so a
    corrupt record degrades to zeros/None instead of raising.
    """
    usage = usage or {}
    raw_channels = usage.get("channels") or {}
    channels = {}
    for uuid, record in raw_channels.items():
        record = record or {}
        channels[uuid] = {
            "watch_count": _coerce_int(record.get("watch_count")),
            "watch_seconds": _coerce_float(record.get("watch_seconds")),
            "tune_count": _coerce_int(record.get("tune_count")),
            "last_watched": _coerce_timestamp(record.get("last_watched")),
            "last_tuned": _coerce_timestamp(record.get("last_tuned")),
            "first_seen": _coerce_timestamp(record.get("first_seen")),
        }
    return {"channels": channels, "meta": usage.get("meta") or {}}


def _setting_number(settings, key, default, cast):
    """Coerce a user setting (M3).

    `settings.get(key, default) or default` silently turns an intentional 0
    (e.g. top_n=0, "show nothing") into `default` because 0 is falsy. Only a
    genuinely MISSING or empty-string value should fall back to `default`.
    """
    value = settings.get(key, default)
    if value is None or value == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _entry(row, record, reason, now):
    created = row.created_at
    age_days = ((now - created) / 86400.0) if created else None
    return {
        "uuid": row.uuid,
        "name": row.name,
        "group": row.group or "(no group)",
        "watch_count": int(record.get("watch_count") or 0),
        "hours": round(float(record.get("watch_seconds") or 0.0) / 3600.0, 2),
        "last_watched": record.get("last_watched"),
        "last_tuned": record.get("last_tuned"),
        "tune_count": int(record.get("tune_count") or 0),
        "age_days": round(age_days, 1) if age_days is not None else None,
        "reason": reason,
    }


def build_model(rows, usage, settings, now):
    """Join the ORM channel universe (`rows`) against the sparse usage overlay.

    Returns the model dict consumed by `summary_for_webhook` and by Task 9's
    renderers -- see the module docstring for the `counts` sum invariant and
    the `too_new`-is-a-peer design. `group_rollup[*]["total"]` is the group's
    TRUE ORM channel count (every row in that group, including excluded/
    unobservable ones), not just the rows that landed in never/used/tuned_only
    (M2) -- so it can exceed `["never"] + <watched in that group>`.

    A channel whose stream profile is non-proxying ("unobservable") but that
    nonetheless carries real watch history (profile was changed to non-
    proxying AFTER it was watched) is surfaced in the usage rankings rather
    than hidden in the `unobservable` bucket (M1).
    """
    usage = _sanitize_usage(usage)
    channels = usage["channels"]
    meta = usage["meta"]
    exclusions = gateway.parse_exclusions(settings)

    top_n = _setting_number(settings, "top_n", 20, int)
    threshold_days = _setting_number(settings, "unused_threshold_days", 30, float)

    never, tuned_only, used, excluded, unobservable = [], [], [], [], []

    for row in rows:
        reason = gateway.classify(row, exclusions, now)
        # THE INVARIANT: a channel absent from usage.channels is never-watched,
        # not an error -- .get() with an EMPTY default, never a KeyError.
        record = channels.get(row.uuid) or EMPTY

        if reason == "unobservable" and record.get("watch_count", 0) > 0:
            # M1: real usage survives a later profile change to non-proxying.
            used.append(_entry(row, record, "watched", now))
            continue
        if reason == "unobservable":
            unobservable.append(_entry(row, record, reason, now))
            continue
        if reason:
            excluded.append(_entry(row, record, reason, now))
            continue

        if record.get("watch_count", 0) > 0:
            used.append(_entry(row, record, "watched", now))
        elif record.get("tune_count", 0) > 0:
            # Tried and abandoned inside min_watch_seconds: BROKEN, not unused.
            tuned_only.append(_entry(row, record, "tuned_never_qualified", now))
        else:
            age = ((now - row.created_at) / 86400.0) if row.created_at else None
            too_new = age is not None and age < threshold_days
            never.append(_entry(row, record, "too_new" if too_new
                                else "never_watched", now))

    used.sort(key=lambda c: (c["watch_count"], c["hours"]), reverse=True)

    group_totals = {}
    for row in rows:
        group = row.group or "(no group)"
        group_totals[group] = group_totals.get(group, 0) + 1

    rollup = {}
    for entry in never + used + tuned_only:
        bucket = rollup.setdefault(entry["group"], {"group": entry["group"],
                                                    "never": 0, "total": 0})
        if entry["reason"] in ("never_watched", "too_new"):
            bucket["never"] += 1
    for bucket in rollup.values():
        bucket["total"] = group_totals.get(bucket["group"], 0)
    group_rollup = sorted(rollup.values(), key=lambda g: g["never"], reverse=True)

    never_watched_entries = [e for e in never if e["reason"] == "never_watched"]
    too_new_entries = [e for e in never if e["reason"] == "too_new"]

    gate = gates.evaluate(usage, rows_total=len(rows),
                          never_watched=len(never_watched_entries), now=now,
                          thresholds=settings)
    # C1: a non-proxying stream profile never writes Redis keys, so those
    # channels are structurally invisible to the collector -- if most of the
    # lineup is unobservable, the dataset cannot support any conclusion.
    # evaluate() has no visibility into this count; wire it in here.
    unobservable_msg = gates.unobservable_alert(len(unobservable), len(rows))
    if unobservable_msg:
        gate["alerts"].append(unobservable_msg)
        gate["ok"] = False

    stats_since = meta.get("stats_since")
    tracked_days = ((now - float(stats_since)) / 86400.0) if stats_since else 0.0

    # I1: disjoint from most_used -- take up to top_n from the tail, but never
    # more than what's left over once the head (most_used) claims its share.
    least_n = max(0, min(top_n, len(used) - top_n))

    return {
        "generated_at": now,
        "generated_at_local": time.strftime("%Y-%m-%d %H:%M %Z",
                                            time.localtime(now)),
        "stats_since": stats_since,
        "tracked_days": round(tracked_days, 1),
        "coverage": gate["coverage"],
        "total_channels": len(rows),
        "never_watched": never,
        "tuned_never_qualified": tuned_only,
        "most_used": used[:top_n],
        "least_used": list(reversed(used))[:least_n],
        "excluded": excluded,
        "unobservable": unobservable,
        "group_rollup": group_rollup,
        "gate": gate,
        "counts": {
            "never_watched": len(never_watched_entries),
            "too_new": len(too_new_entries),
            "tuned_never_qualified": len(tuned_only),
            "watched": len(used),
            "excluded": len(excluded),
            "unobservable": len(unobservable),
        },
    }


def summary_for_webhook(model, report_url):
    return {
        "tracked_days": model["tracked_days"],
        "coverage": model["coverage"],
        "total_channels": model["total_channels"],
        "never_watched": model["counts"]["never_watched"],
        "tuned_never_qualified": model["counts"]["tuned_never_qualified"],
        "top": [{"name": c["name"], "watch_count": c["watch_count"],
                 "hours": c["hours"]} for c in model["most_used"][:5]],
        "report_url": report_url,
        "alerts": model["gate"]["alerts"],
    }
