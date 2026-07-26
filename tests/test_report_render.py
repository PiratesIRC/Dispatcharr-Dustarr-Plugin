import csv
import io
import os
import time

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


def test_html_never_watched_heading_matches_its_table_and_excludes_too_new(rp, gw):
    """R2: the 'Never watched' heading count must equal the row count in the
    table beneath it, and a too_new channel (needs more time, not action)
    must never appear under that heading -- it gets its own section."""
    stale = [gw.ChannelRow(id=i, uuid=f"s{i}", name=f"STALE{i}", group="US: Movies",
                           auto_created=False, created_at=NOW - 90 * 86400,
                           proxying=True) for i in range(2)]
    fresh = [gw.ChannelRow(id=100 + i, uuid=f"f{i}", name=f"FRESH{i}", group="US: Movies",
                          auto_created=False, created_at=NOW - 3 * 86400,
                          proxying=True) for i in range(3)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    settings = dict(SETTINGS, unused_threshold_days=30)
    built = rp.build_model(stale + fresh, usage, settings, NOW)
    assert built["counts"]["never_watched"] == 2
    assert built["counts"]["too_new"] == 3

    html = rp.render_html(built)
    never_heading = html.index("Never watched")
    too_new_heading = html.index("Too new to judge")
    assert never_heading < too_new_heading

    never_section = html[never_heading:too_new_heading]
    # R2 still holds: the count in the summary equals the rows beneath it.
    assert '<span class="count">2</span>' in never_section
    for i in range(3):
        assert f">FRESH{i}<" not in never_section
    for i in range(2):
        assert never_section.count(f">STALE{i}<") == 1


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


# -- I4: report_base_url turns the bare report path into a real link ---------

def test_full_report_url_falls_back_to_the_bare_path_when_base_is_empty(rp):
    assert rp.full_report_url("", rp.REPORT_URL_PATH) == rp.REPORT_URL_PATH
    assert rp.full_report_url(None, rp.REPORT_URL_PATH) == rp.REPORT_URL_PATH
    assert rp.full_report_url("   ", rp.REPORT_URL_PATH) == rp.REPORT_URL_PATH


def test_full_report_url_joins_base_and_path(rp):
    assert (rp.full_report_url("http://192.168.1.53:9191", rp.REPORT_URL_PATH)
            == "http://192.168.1.53:9191" + rp.REPORT_URL_PATH)


def test_full_report_url_strips_a_trailing_slash_on_base(rp):
    assert (rp.full_report_url("http://host:9191/", rp.REPORT_URL_PATH)
            == "http://host:9191" + rp.REPORT_URL_PATH)


# -- M1: last-watched is the highest-value signal in the dataset -------------

def test_html_includes_a_last_watched_column(rp, gw):
    rows = [gw.ChannelRow(id=1, uuid="u1", name="CH1", group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)]
    channels = {"u1": {"watch_count": 3, "watch_seconds": 7200.0, "tune_count": 3,
                       "last_watched": NOW - 3600, "last_tuned": NOW - 3600,
                       "first_seen": NOW - 80 * 86400}}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    html = rp.render_html(rp.build_model(rows, usage, SETTINGS, NOW))
    assert "Last watched" in html
    assert rp._fmt_local(NOW - 3600) in html


def test_csv_last_watched_and_last_tuned_are_iso8601_not_raw_epoch(rp, gw):
    """An Excel/LibreOffice user must see a real date, not `1752...`."""
    rows = [gw.ChannelRow(id=1, uuid="u1", name="CH1", group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)]
    channels = {"u1": {"watch_count": 3, "watch_seconds": 7200.0, "tune_count": 3,
                       "last_watched": NOW - 3600, "last_tuned": NOW - 1800,
                       "first_seen": NOW - 80 * 86400}}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    built = rp.build_model(rows, usage, SETTINGS, NOW)
    text = rp.render_csv(built)
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["last_watched"] == time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(NOW - 3600))
    assert row["last_tuned"] == time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(NOW - 1800))
    assert "." not in row["last_watched"]     # not a raw float


def test_csv_blank_last_watched_stays_blank_not_a_bogus_epoch_date(rp, gw):
    text = rp.render_csv(model(rp, gw, n=3, watched=0))
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["last_watched"] == ""


# -- M4: "Least used" silently rendering "None." is baffling, not correct ----

def test_least_used_notes_that_all_watched_channels_are_listed_above(rp, gw):
    html = rp.render_html(model(rp, gw, n=5, watched=2))   # top_n=5 >= watched=2
    least_at = html.index("Least used")
    most_at = html.index("Most used")
    section = html[least_at:most_at]
    assert "all watched channels are listed above" in section.lower()


def test_least_used_has_no_note_when_it_actually_has_entries(rp, gw):
    html = rp.render_html(model(rp, gw, n=20, watched=20))  # top_n=5 << watched=20
    least_at = html.index("Least used")
    most_at = html.index("Most used")
    section = html[least_at:most_at]
    assert "all watched channels are listed above" not in section.lower()


# -- FIX 1: CSV formula injection (CWE-1236) --------------------------------
#
# Provider-controlled channel/group names beginning with =, +, -, or @ (or
# leading whitespace/tab then one of those) are interpreted by Excel /
# LibreOffice as formulas when the CSV is double-clicked open. Every such
# cell must be neutralized with a leading single quote; ordinary names and
# genuinely negative *numbers* in numeric columns must survive unmangled.
_FORMULA_PAYLOADS = ["=1+1", "+1+1", "-1+1", "@SUM(1+1)", "\t=1+1"]


def _model_with_channel_name(rp, gw, name):
    rows = [gw.ChannelRow(id=1, uuid="u1", name=name, group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    return rp.build_model(rows, usage, SETTINGS, NOW)


@pytest.mark.parametrize("payload", _FORMULA_PAYLOADS)
def test_csv_neutralizes_formula_injection_in_channel_name(rp, gw, payload):
    text = rp.render_csv(_model_with_channel_name(rp, gw, payload))
    row = next(csv.DictReader(io.StringIO(text)))
    # Neutralized: a leading single quote defeats Excel/LibreOffice formula
    # evaluation while keeping the text visible.
    assert row["name"] == "'" + payload


@pytest.mark.parametrize("payload", _FORMULA_PAYLOADS)
def test_csv_neutralizes_formula_injection_in_group_name(rp, gw, payload):
    rows = [gw.ChannelRow(id=1, uuid="u1", name="CH1", group=payload,
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    text = rp.render_csv(rp.build_model(rows, usage, SETTINGS, NOW))
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["group"] == "'" + payload


def test_csv_leaves_ordinary_channel_names_untouched(rp, gw):
    text = rp.render_csv(_model_with_channel_name(rp, gw, "BBC 1 FHD"))
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["name"] == "BBC 1 FHD"


def test_csv_does_not_mangle_negative_numbers_in_numeric_columns(rp, gw):
    """age_days is a numeric column and can legitimately be written; only
    STRING cells (name/group/reason) are candidates for neutralization, so a
    negative number must round-trip as a plain number, not gain a quote."""
    rows = [gw.ChannelRow(id=1, uuid="u1", name="CH1", group="US: Movies",
                          auto_created=False, created_at=NOW + 5 * 86400,
                          proxying=True)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    built = rp.build_model(rows, usage, SETTINGS, NOW)
    assert built["never_watched"][0]["age_days"] < 0
    text = rp.render_csv(built)
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["age_days"] == str(built["never_watched"][0]["age_days"])
    assert not row["age_days"].startswith("'")


# -- FIX 2: atomic-write guarantee on os.replace failure --------------------
#
# _atomic_write must never leave a stale .tmp file behind, including when
# os.replace() itself fails (disk full / permission / locked file). These
# tests pin the tmp+replace MECHANISM (not just cleanup behavior): they
# monkeypatch os.replace to fail, so a rewrite that skipped the tmp+replace
# dance entirely (writing straight to the final path) would never trigger
# the failure and would fail these assertions instead of vacuously passing.
def test_atomic_write_removes_tmp_file_when_replace_fails(rp, tmp_path, monkeypatch):
    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(rp.os, "replace", boom)
    target = tmp_path / "out.txt"
    with pytest.raises(OSError):
        rp._atomic_write(str(target), "hello")
    assert not list(tmp_path.glob("*.tmp"))
    assert not target.exists()


def test_write_report_survives_a_replace_failure_with_no_stale_tmp(rp, gw, tmp_path,
                                                                    monkeypatch):
    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(rp.os, "replace", boom)
    report_dir = tmp_path / "logos"
    csv_dir = tmp_path / "config"
    out = rp.write_report(model(rp, gw), str(report_dir), str(csv_dir), NOW)
    assert "error" in out
    assert not list(report_dir.glob("*.tmp"))
    assert not list(csv_dir.glob("*.tmp"))


# -- archive path returned + both archive streams bounded (keep 8) ----------

def test_write_report_returns_the_archive_path(rp, gw, tmp_path):
    out = rp.write_report(model(rp, gw), str(tmp_path / "r"), str(tmp_path / "c"),
                          1_752_770_000.0)
    assert out["archive_path"] and out["archive_path"] != out["html_path"]
    assert os.path.basename(out["archive_path"]).startswith("report-")
    assert os.path.exists(out["archive_path"])


def test_archives_are_pruned_to_the_bound(rp, gw, tmp_path):
    rdir, cdir = str(tmp_path / "r"), str(tmp_path / "c")
    for i in range(rp.ARCHIVE_KEEP + 3):
        rp.write_report(model(rp, gw), rdir, cdir, 1_752_770_000.0 + i * 60)
    html = [n for n in os.listdir(rdir) if n.startswith("report-")]
    csvs = [n for n in os.listdir(cdir) if n.startswith("report-")]
    assert len(html) == rp.ARCHIVE_KEEP
    assert len(csvs) == rp.ARCHIVE_KEEP
    assert os.path.exists(os.path.join(rdir, "report.html"))  # live survives


def test_prune_never_touches_the_live_report(rp, gw, tmp_path):
    rdir = str(tmp_path / "r")
    rp.write_report(model(rp, gw), rdir, str(tmp_path / "c"), 1_752_770_000.0)
    assert "report.html" in os.listdir(rdir)
