"""Metricsarr reports -- join the ORM channel universe against usage.json.

THE INVARIANT (spec S6): the channel universe is the ORM. usage.json is a SPARSE
OVERLAY of watched channels only. A channel absent from it is NEVER-WATCHED --
the default, expected state, not an error. That is the plugin's entire purpose.
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
    usage = usage or {}
    channels = usage.get("channels") or {}
    meta = usage.get("meta") or {}
    exclusions = gateway.parse_exclusions(settings)

    top_n = int(settings.get("top_n", 20) or 20)
    threshold_days = float(settings.get("unused_threshold_days", 30) or 30)

    never, tuned_only, used, excluded, unobservable = [], [], [], [], []

    for row in rows:
        reason = gateway.classify(row, exclusions, now)
        # THE INVARIANT: a channel absent from usage.channels is never-watched,
        # not an error -- .get() with an EMPTY default, never a KeyError.
        record = channels.get(row.uuid) or EMPTY

        if reason == "unobservable":
            unobservable.append(_entry(row, record, reason, now))
            continue
        if reason:
            excluded.append(_entry(row, record, reason, now))
            continue

        if record.get("watch_count"):
            used.append(_entry(row, record, "watched", now))
        elif record.get("tune_count"):
            # Tried and abandoned inside min_watch_seconds: BROKEN, not unused.
            tuned_only.append(_entry(row, record, "tuned_never_qualified", now))
        else:
            age = ((now - row.created_at) / 86400.0) if row.created_at else None
            too_new = age is not None and age < threshold_days
            never.append(_entry(row, record, "too_new" if too_new
                                else "never_watched", now))

    used.sort(key=lambda c: (c["watch_count"], c["hours"]), reverse=True)

    rollup = {}
    for entry in never + used + tuned_only:
        bucket = rollup.setdefault(entry["group"], {"group": entry["group"],
                                                    "never": 0, "total": 0})
        bucket["total"] += 1
        if entry["reason"] in ("never_watched", "too_new"):
            bucket["never"] += 1
    group_rollup = sorted(rollup.values(), key=lambda g: g["never"], reverse=True)

    gate = gates.evaluate(usage, rows_total=len(rows), never_watched=len(never),
                          now=now, thresholds=settings)

    stats_since = meta.get("stats_since")
    tracked_days = ((now - float(stats_since)) / 86400.0) if stats_since else 0.0

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
        "least_used": list(reversed(used))[:top_n],
        "excluded": excluded,
        "unobservable": unobservable,
        "group_rollup": group_rollup,
        "gate": gate,
        "counts": {
            "never_watched": len(never),
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
