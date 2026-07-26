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
added to a matcher). A dataset that is still young and not-ok is exactly the
F1 warmup case and must never trigger a critical; once it crosses the
window, any remaining not-ok state can only be sensor/mass-casualty shaped,
because the warmup alert would have cleared by then.

FINAL-REVIEW FIX (F1 rounding seam): this used to be
`sensor_blind = (not gate.ok) and tracked_days >= window`, comparing against
`model["tracked_days"]`, which `reports.build_model` stores as
`round(age_days, 1)`. For a raw age in [~29.955, 30.0), rounding pushes
`tracked_days` up to exactly the window while the dataset is still, in
reality, immature -- so a healthy household mid-warmup got paged with a
false CRITICAL "usage sensor not trustworthy". Maturity is now read
straight from `gate["immature"]`, which `gates.evaluate()` computes from its
own UN-ROUNDED `age_days` (the same value that drives the warmup alert
text), so the two can never disagree. `sensor_blind` no longer touches
`tracked_days` or `thresholds` at all; `thresholds` stays a parameter only
for call-site signature compatibility. An absent `immature` key (a
malformed/legacy model, or one built by a caller that predates this fix)
reads as immature -- never page -- because a false critical is exactly the
failure this fix exists to remove, so the unknown case must fail toward
silence, not toward paging.
"""
from __future__ import annotations

import json
import os

STATE_FILE = "notify_state.json"
_DEDUP_KEY = "honesty_gate:report"


def sensor_blind(model, thresholds):
    """`thresholds` is accepted for call-site signature compatibility only
    and is otherwise unused: the maturity decision moved to the gate's own
    un-rounded `immature` field (see the FINAL-REVIEW FIX note above), which
    already incorporates `unused_threshold_days` -- re-deriving it here from
    `thresholds` + `model["tracked_days"]` is exactly the rounding seam that
    fix removes.
    """
    gate = (model or {}).get("gate") or {}
    if gate.get("ok", True):
        return False
    # Absent `immature` (malformed/legacy model) reads as immature: never
    # page. A false critical is the worse failure -- this is F1's thesis.
    return not gate.get("immature", True)


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
    """Bool-only wrapper, kept so existing callers and their tests are
    unchanged. New code wanting the failure REASON calls emit_report_result."""
    return emit_report_result(notify_fn, summary, url, attachment_path)[0]


def emit_report_result(notify_fn, summary, url, attachment_path):
    """-> (ok, reason_or_None).

    The reason matters because `notify()` NEVER RAISES -- it returns False when
    the spool refuses the event -- so without this the operator is told "not
    emitted" with no cause, which is only half of the silence closed.

    The reason carries an exception's TYPE NAME only, never `str(exc)`:
    provider credentials live inside stream URLs in this deployment, and this
    string is rendered to the operator.
    """
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
        if notify_fn(**kwargs):
            return True, None
        return False, ("Newsflasharr declined the event (spool full, or the "
                       "spool could not be written)")
    except Exception as exc:
        return False, f"emit failed: {type(exc).__name__}"


def emit_gate(notify_fn, model, thresholds, prev_ok):
    try:
        if sensor_blind(model, thresholds):
            alerts = ((model or {}).get("gate") or {}).get("alerts") or []
            sent = notify_fn(source="metricsarr", event="honesty_gate",
                             severity="critical", kind="event",
                             dedup_key=_DEDUP_KEY,
                             title="Metricsarr: usage sensor not trustworthy",
                             body="\n".join(str(a) for a in alerts))
            # notify() NEVER RAISES -- it RETURNS False when the spool refuses
            # it (spool full, redaction failure, Newsflasharr not installed).
            # Recording an alert that was never spooled is worse than a
            # duplicate: the state then says "outstanding", so a later recovery
            # emits a RESOLVE for a critical the operator never received.
            # Leaving prev_ok untouched retries on the next scheduled run.
            if not sent:
                return bool(prev_ok), None
            return False, "alert"
        if not prev_ok:
            sent = notify_fn(source="metricsarr", event="honesty_gate",
                             severity="info", kind="resolve",
                             dedup_key=_DEDUP_KEY,
                             title="Metricsarr: usage sensor trustworthy again",
                             body="honesty gate passing again")
            # Same rule the other way round: a resolve that never landed must
            # leave the alert OUTSTANDING, or the pairing is lost in silence.
            if not sent:
                return False, None
            return True, "resolve"
        return True, None
    except Exception:
        return bool(prev_ok), None
