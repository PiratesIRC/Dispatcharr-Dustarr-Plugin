"""Pure emit + gate-state logic for Metricsarr's Newsflasharr integration.

Consumes the report `model` dict (`model["gate"]["ok"|"alerts"]`,
`model["tracked_days"]`, `model["counts"]`, `model["coverage"]`,
`model["total_channels"]`) and a `summary` dict (`reports.summary_for_notify`:
tracked_days, coverage, total_channels, never_watched,
tuned_never_qualified, top, report_url, alerts). Produces the two builder
functions a later task wires into the Celery report task, plus tiny
file-backed state for the honesty-gate alert/resolve pairing. Stdlib only,
no imports of any other metricsarr module, no Django -- `notify_fn` is
injected so this module never imports notify_client either.

SENSOR-BLIND CLASSIFICATION (spec Sec6.1 / rev-1 error 3) -- why this needs
no string matching:

`gates.evaluate()` can render `ok=False` for reasons that fall into two
completely different classes once you ask "does this mean the SENSOR is
broken, or that the HOUSEHOLD is idle?":

  * Sensor/mass-casualty shaped (every one of these survives, and keeps
    firing, once the dataset is mature -- i.e. `tracked_days >=
    unused_threshold_days`): empty data, missing `stats_since` (unreachable
    once mature), a coverage collapse, no recent qualified watches, too few
    distinct channels observed, hitting the never-watched ceiling, an empty
    channel lineup, an unobservable fraction of channels. None of these is
    "the household went on vacation" -- they are all shaped like "the
    collector stopped seeing reality."
  * Immature/warmup shaped: the ONE alert that reads "only N days of data
    (need M)". This is BY DESIGN (F1) -- a dataset younger than the
    unused-threshold window has not had a chance to prove anything yet, and
    it is the only alert that DISAPPEARS once `tracked_days` reaches the
    window. It must never page.

Because the immature case is the only one that self-resolves with time and
every other case does not, classifying by AGE rather than by matching alert
TEXT is both simpler and more robust (new alert strings never need to be
added to a matcher): `sensor_blind = (not gate.ok) and tracked_days >=
window`. A dataset that is still young and not-ok is exactly the F1
warmup case and must never trigger a critical; once it crosses the window,
any remaining not-ok state can only be sensor/mass-casualty shaped, because
the warmup alert would have cleared by then.
"""
from __future__ import annotations

import json
import os

STATE_FILE = "notify_state.json"
_DEDUP_KEY = "honesty_gate:report"


def sensor_blind(model, thresholds):
    gate = (model or {}).get("gate") or {}
    if gate.get("ok", True):
        return False
    window = float((thresholds or {}).get("unused_threshold_days", 30))
    try:
        tracked = float(model.get("tracked_days") or 0)
    except (TypeError, ValueError):
        return False          # unreadable age reads as immature: never page
    return tracked >= window


def load_prev_ok(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(json.load(fh).get("prev_ok", True))
    except Exception:
        return True           # missing/corrupt == "no alert outstanding"


def save_prev_ok(path, ok):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"prev_ok": bool(ok)}, fh)
        os.replace(tmp, path)
    except Exception:
        pass                  # state loss degrades to a duplicate alert, never a crash


def emit_report(notify_fn, summary, url, attachment_path):
    try:
        s = summary or {}
        lines = [
            f"{s.get('never_watched', 0)} of {s.get('total_channels', 0)} "
            f"channels never watched",
            f"tracking {s.get('tracked_days', 0)} days, "
            f"coverage {float(s.get('coverage') or 0):.0%}",
        ]
        if s.get("tuned_never_qualified"):
            lines.append(f"{s['tuned_never_qualified']} tuned but never "
                         f"qualified - likely broken")
        for alert in (s.get("alerts") or []):
            lines.append(f"! {alert}")
        kwargs = {"source": "metricsarr", "event": "usage_report",
                  "severity": "info", "kind": "event",
                  "title": "Metricsarr usage report",
                  "body": "\n".join(lines)}
        if url:
            kwargs["url"] = url
        if attachment_path:
            kwargs["attachment"] = attachment_path
        return bool(notify_fn(**kwargs))
    except Exception:
        return False


def emit_gate(notify_fn, model, thresholds, prev_ok):
    try:
        if sensor_blind(model, thresholds):
            alerts = ((model or {}).get("gate") or {}).get("alerts") or []
            notify_fn(source="metricsarr", event="honesty_gate",
                      severity="critical", kind="event",
                      dedup_key=_DEDUP_KEY,
                      title="Metricsarr: usage sensor not trustworthy",
                      body="\n".join(str(a) for a in alerts))
            return False, "alert"
        if not prev_ok:
            notify_fn(source="metricsarr", event="honesty_gate",
                      severity="info", kind="resolve",
                      dedup_key=_DEDUP_KEY,
                      title="Metricsarr: usage sensor trustworthy again",
                      body="honesty gate passing again")
            return True, "resolve"
        return True, None
    except Exception:
        return bool(prev_ok), None
