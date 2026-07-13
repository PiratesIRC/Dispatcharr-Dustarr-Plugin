import csv
import io

import pytest
from conftest import load_plugin

load_plugin()

NOW = 1_700_000_000.0


@pytest.fixture()
def rp():
    import sys
    return sys.modules["metricsarr_under_test.reports"]


@pytest.fixture()
def gw():
    import sys
    return sys.modules["metricsarr_under_test.gateway"]


SETTINGS = {"exclude_groups": "", "exclude_name_regex": "",
            "exclude_auto_created": False, "top_n": 5,
            "unused_threshold_days": 30, "never_watched_ceiling": 0.99,
            "poll_interval_s": 15.0, "client_gap_grace_s": 90.0}


def model(rp, gw, n=5, watched=2):
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True) for i in range(n)]
    channels = {f"u{i}": {"watch_count": 3, "watch_seconds": 7200.0,
                          "tune_count": 3, "last_watched": NOW - 3600,
                          "last_tuned": NOW - 3600, "first_seen": NOW - 80 * 86400}
                for i in range(watched)}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    return rp.build_model(rows, usage, SETTINGS, NOW)


def test_html_is_self_contained(rp, gw):
    html = rp.render_html(model(rp, gw))
    # A strict CSP / an offline Shield browser must render it: no external assets.
    for forbidden in ("<script src=", "<link rel=\"stylesheet\"", "http://cdn",
                      "https://cdn", "@import"):
        assert forbidden not in html
    assert "<style>" in html
    assert html.strip().startswith("<!doctype html>")


def test_html_leads_with_never_watched(rp, gw):
    html = rp.render_html(model(rp, gw))
    never_at = html.index("Never watched")
    most_at = html.index("Most used")
    # The dead weight is the ask; the top-N is a nice-to-have.
    assert never_at < most_at


def test_html_shows_data_confidence_header(rp, gw):
    html = rp.render_html(model(rp, gw))
    assert "Tracking since" in html
    assert "coverage" in html.lower()


def test_html_escapes_channel_names(rp, gw):
    rows = [gw.ChannelRow(id=1, uuid="u1", name='<script>alert("x")</script>',
                          group="US: Movies", auto_created=False,
                          created_at=NOW - 90 * 86400, proxying=True)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400}}
    html = rp.render_html(rp.build_model(rows, usage, SETTINGS, NOW))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_html_renders_the_gate_banner_when_data_is_untrustworthy(rp, gw):
    html = rp.render_html(model(rp, gw, n=5, watched=0))
    assert "banner" in html
    assert "blind" in html.lower()


def test_html_includes_the_tuned_never_qualified_section(rp, gw):
    html = rp.render_html(model(rp, gw))
    assert "Tuned but never qualified" in html


def test_csv_has_one_row_per_channel_with_a_reason(rp, gw):
    text = rp.render_csv(model(rp, gw, n=5, watched=2))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 5
    assert {"uuid", "name", "group", "watch_count", "hours", "reason"} <= set(rows[0])
    assert {r["reason"] for r in rows} == {"watched", "never_watched"}


def test_write_report_creates_both_files_and_returns_the_url(rp, gw, tmp_path):
    report_dir = tmp_path / "logos"
    csv_dir = tmp_path / "config"
    out = rp.write_report(model(rp, gw), str(report_dir), str(csv_dir), NOW)
    assert (report_dir / "report.html").exists()
    assert list(csv_dir.glob("report-*.csv"))
    assert out["url"] == rp.REPORT_URL_PATH
    assert "error" not in out
    # No tmp files left behind.
    assert not list(report_dir.glob("*.tmp"))


def test_write_report_survives_an_unwritable_csv_dir(rp, gw, tmp_path, monkeypatch):
    """The HTML report is the product; a failed CSV write must degrade, not raise."""
    real_makedirs = rp.os.makedirs

    def selective(path, *args, **kwargs):
        if "config" in str(path):
            raise OSError("read-only")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(rp.os, "makedirs", selective)
    out = rp.write_report(model(rp, gw), str(tmp_path / "logos"),
                          str(tmp_path / "config"), NOW)
    assert (tmp_path / "logos" / "report.html").exists()
    assert out["csv_path"] is None
    assert "csv" in out["error"].lower()


def test_write_report_keeps_a_dated_archive(rp, gw, tmp_path):
    report_dir = tmp_path / "logos"
    rp.write_report(model(rp, gw), str(report_dir), str(tmp_path / "config"), NOW)
    assert list(report_dir.glob("report-*.html"))
