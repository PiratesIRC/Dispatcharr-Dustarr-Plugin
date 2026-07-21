import pytest
from conftest import load_pure


@pytest.fixture()
def nr():
    return load_pure("notify_report")


def _model(ok=True, alerts=(), tracked=45):
    return {"gate": {"ok": ok, "alerts": list(alerts)},
            "tracked_days": tracked, "coverage": 0.97,
            "total_channels": 1440,
            "counts": {"never_watched": 400, "tuned_never_qualified": 20}}


TH = {"unused_threshold_days": 30}


def test_a_young_dataset_never_pages(nr):
    # F1 regression lock: warmup not-ok is BY DESIGN, never a critical.
    m = _model(ok=False, alerts=["only 7 days of data (need 30)"], tracked=7)
    assert nr.sensor_blind(m, TH) is False


def test_a_mature_not_ok_gate_pages(nr):
    m = _model(ok=False, alerts=["no qualified watches in the last 7 days"],
                tracked=45)
    assert nr.sensor_blind(m, TH) is True


def test_a_mature_ok_gate_does_not_page(nr):
    assert nr.sensor_blind(_model(ok=True, tracked=45), TH) is False


def test_exactly_at_the_window_pages(nr):   # gates convention: pass AT threshold
    m = _model(ok=False, alerts=["x"], tracked=30)
    assert nr.sensor_blind(m, TH) is True


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
                          "/data/logos/metricsarr/report-1.html") is True
    kw = calls[0]
    assert kw["source"] == "metricsarr" and kw["event"] == "usage_report"
    assert kw["severity"] == "info" and "dedup_key" not in kw
    assert kw["attachment"] == "/data/logos/metricsarr/report-1.html"
    assert "400" in kw["body"]                  # counts reach the body

    def boom(**kw):
        raise RuntimeError("x")

    assert nr.emit_report(boom, summary, None, None) is False  # never raises
