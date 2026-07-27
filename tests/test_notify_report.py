import time as _time

import pytest
from conftest import load_pure


@pytest.fixture()
def nr():
    return load_pure("notify_report")


@pytest.fixture()
def gates_mod():
    return load_pure("gates")


def _model(ok=True, alerts=(), tracked=45, immature=False):
    return {"gate": {"ok": ok, "alerts": list(alerts), "immature": immature},
            "tracked_days": tracked, "coverage": 0.97,
            "total_channels": 1440,
            "counts": {"never_watched": 400, "tuned_never_qualified": 20}}


TH = {"unused_threshold_days": 30}


def _buckets(now, hours, ticks, max_gap=15.0):
    out = {}
    for i in range(hours):
        key = _time.strftime("%Y-%m-%dT%H", _time.gmtime(now - i * 3600))
        out[key] = {"ticks": ticks, "max_gap_s": max_gap}
    return out


def _usage_with(now, watched_channels, stats_since, poll_interval_s=15.0):
    channels = {}
    for i in range(watched_channels):
        channels[f"u{i}"] = {"watch_count": 3, "watch_seconds": 3600.0,
                             "tune_count": 3, "last_watched": now - 3600,
                             "last_tuned": now - 3600, "first_seen": now - 86400}
    needed_ticks = int(3600.0 / poll_interval_s) + 5
    return {"channels": channels,
            "meta": {"stats_since": stats_since,
                     "coverage": _buckets(now, 24 * 30, needed_ticks)}}


def test_a_young_dataset_never_pages(nr):
    # F1 regression lock: warmup not-ok is BY DESIGN, never a critical.
    m = _model(ok=False, alerts=["only 7 days of data (need 30)"], tracked=7,
               immature=True)
    assert nr.sensor_blind(m, TH) is False


def test_a_mature_not_ok_gate_pages(nr):
    m = _model(ok=False, alerts=["no qualified watches in the last 7 days"],
                tracked=45, immature=False)
    assert nr.sensor_blind(m, TH) is True


def test_a_mature_ok_gate_does_not_page(nr):
    assert nr.sensor_blind(_model(ok=True, tracked=45, immature=False), TH) is False


def test_exactly_at_the_window_pages(nr):   # gates convention: pass AT threshold
    # Exactly at the window means age_days == window_days, which is NOT
    # < window_days -- so gates.evaluate() reports immature=False here (the
    # not-ok comes from some OTHER alert), and it still pages.
    m = _model(ok=False, alerts=["x"], tracked=30, immature=False)
    assert nr.sensor_blind(m, TH) is True


def test_absent_immature_key_never_pages(nr):
    """Fail-toward-silence lock: a malformed/legacy model with no `immature`
    key at all (e.g. built by code that predates this fix) must read as
    immature and never page -- a false critical is the worse failure, which
    is F1's whole thesis."""
    m = {"gate": {"ok": False, "alerts": ["x"]}, "tracked_days": 45}
    assert nr.sensor_blind(m, TH) is False


def test_seam_regression_borderline_age_never_pages(nr, gates_mod):
    """F1 rounding seam, the finding this fix closes.

    Raw age of 29.96 days against a 30-day window rounds UP to
    `tracked_days == 30.0` (reports.build_model stores round(age_days, 1)),
    but the dataset genuinely IS still immature (29.96 < 30). The old
    `sensor_blind` compared the ROUNDED tracked_days against the window and
    paged; the fix reads gates.evaluate()'s own un-rounded `immature` field
    instead, so the two can never disagree.
    """
    now = 1_700_000_000.0
    window_days = 30
    age_days = 29.96
    stats_since = now - age_days * 86400.0
    usage = _usage_with(now, watched_channels=10, stats_since=stats_since)
    thresholds = {"poll_interval_s": 15.0, "client_gap_grace_s": 90.0,
                  "unused_threshold_days": window_days,
                  "never_watched_ceiling": 0.98}
    gate = gates_mod.evaluate(usage, rows_total=1440, never_watched=400,
                              now=now, thresholds=thresholds, judged_total=410)
    assert gate["ok"] is False               # the still-immature alert fired
    assert gate["immature"] is True           # the un-rounded truth

    model = {"gate": gate, "tracked_days": round(age_days, 1)}
    assert model["tracked_days"] == 30.0      # the rounding seam itself
    assert nr.sensor_blind(model, TH) is False


def test_emit_gate_alert_then_resolve_pairing(nr):
    calls = []

    def fn(**kw):
        calls.append(kw)
        return True

    blind = _model(ok=False, alerts=["the sensor is blind"], tracked=45)
    ok = _model(ok=True, tracked=45)
    prev, action = nr.emit_gate(fn, blind, TH, prev_ok=True)
    assert (prev, action) == (False, "alert")
    assert calls[-1]["severity"] == "critical"
    assert calls[-1]["dedup_key"] == "honesty_gate:report"
    prev, action = nr.emit_gate(fn, ok, TH, prev_ok=False)
    assert (prev, action) == (True, "resolve")
    assert calls[-1]["kind"] == "resolve"
    assert calls[-1]["dedup_key"] == "honesty_gate:report"
    prev, action = nr.emit_gate(fn, ok, TH, prev_ok=True)
    assert (prev, action) == (True, None)
    assert len(calls) == 2                      # no third emit


def test_state_degrades_to_no_alert_outstanding(nr, tmp_path):
    # F6: missing OR corrupt -> prev_ok True; save round-trips; never raises.
    p = str(tmp_path / "notify_state.json")
    assert nr.load_prev_ok(p) is True                       # missing
    (tmp_path / "notify_state.json").write_bytes(b"{corrupt")
    assert nr.load_prev_ok(p) is True                       # corrupt
    nr.save_prev_ok(p, False)
    assert nr.load_prev_ok(p) is False                      # round-trip


def test_emit_report_carries_the_attachment_and_never_raises(nr):
    calls = []

    def fn(**kw):
        calls.append(kw)
        return True

    summary = {"tracked_days": 12, "coverage": 0.96, "total_channels": 1440,
               "never_watched": 400, "tuned_never_qualified": 20,
               "top": [], "report_url": "http://x/r.html", "alerts": []}
    assert nr.emit_report(fn, summary, "http://x/report-1.html",
                          "/data/logos/dustarr/report-1.html") is True
    kw = calls[0]
    assert kw["source"] == "dustarr" and kw["event"] == "usage_report"
    assert kw["severity"] == "info" and "dedup_key" not in kw
    assert kw["attachment"] == "/data/logos/dustarr/report-1.html"
    assert "400" in kw["body"]                  # counts reach the body

    def boom(**kw):
        raise RuntimeError("x")

    assert nr.emit_report(boom, summary, None, None) is False  # never raises


# -- a refused emit must not advance the gate state ---------------------------
# notify() NEVER RAISES -- it returns False (spool full, refused, redaction
# failure). emit_gate ignored that return, so a critical that was never spooled
# still recorded prev_ok=False ("alert outstanding"). If the gate then recovered,
# the operator got "usage sensor trustworthy again" for a problem they were never
# told about -- and nothing anywhere said the critical had been dropped.

def test_a_refused_alert_does_not_record_an_outstanding_alert(nr):
    calls = []

    def refused(**kw):
        calls.append(kw)
        return False                      # spool refused it; notify never raises

    blind = _model(ok=False, alerts=["the sensor is blind"], tracked=45)
    prev, action = nr.emit_gate(refused, blind, TH, prev_ok=True)
    assert calls, "it must still have tried"
    assert (prev, action) == (True, None), (
        "a critical that was never spooled must not be recorded as outstanding")


def test_a_refused_alert_is_retried_on_the_next_run(nr):
    sent = []

    def flaky(**kw):
        sent.append(kw)
        return len(sent) > 1              # fails once, then succeeds

    blind = _model(ok=False, alerts=["blind"], tracked=45)
    prev, _ = nr.emit_gate(flaky, blind, TH, prev_ok=True)
    assert prev is True                   # not recorded
    prev, action = nr.emit_gate(flaky, blind, TH, prev_ok=prev)
    assert (prev, action) == (False, "alert")
    assert len(sent) == 2


def test_a_refused_resolve_keeps_the_alert_outstanding(nr):
    def refused(**kw):
        return False

    ok = _model(ok=True, tracked=45)
    prev, action = nr.emit_gate(refused, ok, TH, prev_ok=False)
    assert (prev, action) == (False, None), (
        "a resolve that was never spooled must leave the alert outstanding "
        "so the next run retries it")


# -- a refusal must carry a REASON -------------------------------------------
# `_emit_notifications`'s `error` is set only by its OWN except clause, but the
# False return comes from emit_report's blanket except and from notify()
# returning bare False. So in every realistic refusal the reason was None and
# the operator got "not emitted" with no cause.

def test_emit_report_reports_why_the_spool_declined(nr):
    def refused(**kw):
        return False

    ok, reason = nr.emit_report_result(refused, {}, None, None)
    assert ok is False
    assert reason and "declin" in reason.lower()


def test_emit_report_reports_an_exception_by_TYPE_only(nr):
    def boom(**kw):
        raise RuntimeError("http://host/live/secretuser/secretpass/x")

    ok, reason = nr.emit_report_result(boom, {}, None, None)
    assert ok is False
    assert "RuntimeError" in reason
    assert "secretpass" not in reason, "never put str(exc) in a reason"


def test_emit_report_result_has_no_reason_on_success(nr):
    ok, reason = nr.emit_report_result(lambda **kw: True, {}, None, None)
    assert ok is True and reason is None


def test_emit_report_keeps_its_bool_contract(nr):
    """The existing callers and tests keep working: emit_report stays a bool."""
    assert nr.emit_report(lambda **kw: True, {}, None, None) is True
    assert nr.emit_report(lambda **kw: False, {}, None, None) is False
