import csv
import io
import os
import re
import time

import pytest
from conftest import NOW, SETTINGS, csv_data, load_plugin, model

load_plugin()


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


def _model_with_a_watched_excluded_channel(rp, gw):
    """A channel that IS watched but sits in an excluded group. Real shape on
    this box: Fox News and the local OTA affiliates are watched regularly and
    are excluded by policy, so they never reach the usage rankings."""
    rows = [
        gw.ChannelRow(id=1, uuid="u1", name="Fox News", group="US: News",
                      auto_created=False, created_at=NOW - 90 * 86400,
                      proxying=True),
        gw.ChannelRow(id=2, uuid="u2", name="A Movie", group="US: Movies",
                      auto_created=False, created_at=NOW - 90 * 86400,
                      proxying=True),
    ]
    rec = {"watch_count": 3, "watch_seconds": 7200.0, "tune_count": 3,
           "last_watched": NOW - 3600, "last_tuned": NOW - 3600,
           "first_seen": NOW - 80 * 86400}
    usage = {"channels": {"u1": dict(rec), "u2": dict(rec)},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    settings = dict(SETTINGS, exclude_groups="US: News")
    return rp.build_model(rows, usage, settings, NOW)


def test_watched_but_excluded_channels_are_surfaced_in_the_report(rp, gw):
    """`Most used` is drawn only from JUDGED channels, so a watched channel in
    an excluded group never appears in it. Silently omitting real viewing makes
    the rankings read as `what I watch most` when they are only `what I watch
    most among the channels I might turn off`."""
    built = _model_with_a_watched_excluded_channel(rp, gw)
    assert built["counts"]["watched"] == 1          # only the non-excluded one
    assert any(e["watch_count"] > 0 for e in built["excluded"])

    html = rp.render_html(built)
    assert "1 watched channel" in html
    assert "excluded from these rankings" in html


def test_no_exclusion_note_when_nothing_watched_is_excluded(rp, gw):
    """The note must not appear when it would say zero -- a permanent
    parenthetical nobody needs is how real notices get tuned out."""
    html = rp.render_html(model(rp, gw, n=5, watched=2))
    assert "excluded from these rankings" not in html


def test_csv_has_one_row_per_channel_with_a_reason(rp, gw):
    text = rp.render_csv(model(rp, gw, n=5, watched=2))
    rows = list(csv.DictReader(csv_data(text)))
    assert len(rows) == 5
    assert {"uuid", "name", "group", "watch_count", "hours", "reason"} <= set(rows[0])
    assert {r["reason"] for r in rows} == {"watched", "never_watched"}


def test_write_report_creates_both_files_and_returns_their_paths(rp, gw, tmp_path):
    report_dir = tmp_path / "config"
    csv_dir = tmp_path / "config"
    out = rp.write_report(model(rp, gw), str(report_dir), str(csv_dir), NOW)
    assert (report_dir / "report.html").exists()
    assert list(csv_dir.glob("report-*.csv"))
    assert out["html_path"] == str(report_dir / "report.html")
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


def test_write_report_returns_the_live_path_when_only_the_archive_fails(
        rp, gw, tmp_path, monkeypatch):
    """If the disk fills between the live write and the archive write, the
    live report.html IS the new report. Reporting html_path=None with 'html
    write failed' skips the counter and the email while the verify-by-mtime
    artifact says the opposite."""
    real_atomic = rp._atomic_write

    def selective(path, body):
        if "report-" in os.path.basename(path):
            raise OSError("disk full")
        return real_atomic(path, body)

    monkeypatch.setattr(rp, "_atomic_write", selective)
    report_dir = tmp_path / "config"
    out = rp.write_report(model(rp, gw), str(report_dir), str(report_dir), NOW)
    assert (report_dir / "report.html").exists()
    assert out["html_path"] == str(report_dir / "report.html")
    assert out["archive_path"] is None
    assert "archive" in out["error"].lower()


def test_write_report_keeps_a_dated_archive(rp, gw, tmp_path):
    report_dir = tmp_path / "logos"
    rp.write_report(model(rp, gw), str(report_dir), str(tmp_path / "config"), NOW)
    assert list(report_dir.glob("report-*.html"))


# -- The report is not published over HTTP any more --------------------------
#
# It used to be written into /data/logos/, which Dispatcharr's nginx serves to
# the whole LAN with no authentication. These tests are the guard against it
# creeping back: a URL builder is the first thing that would reappear, and a
# report path under /data/logos is the second.

def test_reports_exposes_no_url_builder_or_url_path(rp):
    assert not hasattr(rp, "full_report_url")
    assert not hasattr(rp, "REPORT_URL_PATH")


def test_write_report_returns_no_url_key(rp, gw, tmp_path):
    out = rp.write_report(model(rp, gw), str(tmp_path / "config"),
                          str(tmp_path / "config"), NOW)
    assert "url" not in out


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
    row = next(csv.DictReader(csv_data(text)))
    assert row["last_watched"] == time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(NOW - 3600))
    assert row["last_tuned"] == time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(NOW - 1800))
    assert "." not in row["last_watched"]     # not a raw float


def test_csv_blank_last_watched_stays_blank_not_a_bogus_epoch_date(rp, gw):
    text = rp.render_csv(model(rp, gw, n=3, watched=0))
    row = next(csv.DictReader(csv_data(text)))
    assert row["last_watched"] == ""


def test_a_cold_channel_outside_both_ranking_slices_appears_in_the_csv(rp, gw):
    """Fix 3: render_csv built its rows from a union that did not include
    cold_abandoned/cold_still_tried, and the two ranking lists only carry
    the top and bottom slices of the watched population -- so a cold
    channel ranked in the middle appeared in the HTML report and in no CSV
    row at all. u0 is watched but ranks 6th of 10 by watch_count, outside
    both the top_n=2 head and the top_n=2 tail, and is cold by last_watched."""
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                          auto_created=False, created_at=NOW - 300 * 86400,
                          proxying=True) for i in range(10)]
    watch_counts = {"u1": 90, "u2": 80, "u3": 70, "u4": 60, "u5": 55,
                    "u0": 50, "u6": 45, "u7": 40, "u8": 30, "u9": 20}
    channels = {}
    for uuid, count in watch_counts.items():
        if uuid == "u0":
            channels[uuid] = {"watch_count": count, "watch_seconds": 3600.0,
                              "tune_count": count,
                              "last_watched": NOW - 100 * 86400,
                              "last_tuned": NOW - 100 * 86400,
                              "first_seen": NOW - 250 * 86400}
        else:
            channels[uuid] = {"watch_count": count, "watch_seconds": 3600.0,
                              "tune_count": count, "last_watched": NOW - 3600,
                              "last_tuned": NOW - 3600,
                              "first_seen": NOW - 250 * 86400}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 200 * 86400, "coverage": {}}}
    settings = dict(SETTINGS, top_n=2, recent_window_days=30)
    built = rp.build_model(rows, usage, settings, NOW)

    assert "u0" not in {e["uuid"] for e in built["most_used"]}
    assert "u0" not in {e["uuid"] for e in built["least_used"]}
    assert "u0" in {e["uuid"] for e in built["cold_abandoned"]}

    text = rp.render_csv(built)
    uuids = {row["uuid"] for row in csv.DictReader(csv_data(text))}
    assert "u0" in uuids


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
    row = next(csv.DictReader(csv_data(text)))
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
    row = next(csv.DictReader(csv_data(text)))
    assert row["group"] == "'" + payload


def test_csv_leaves_ordinary_channel_names_untouched(rp, gw):
    text = rp.render_csv(_model_with_channel_name(rp, gw, "BBC 1 FHD"))
    row = next(csv.DictReader(csv_data(text)))
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
    row = next(csv.DictReader(csv_data(text)))
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


# -- Logo, links and section glyphs (2026-08-05) -----------------------------

def test_the_logo_is_embedded_as_a_data_uri_not_linked(rp, gw):
    """This page opens off disk as a file:// URL and is also mailed as an
    attachment. A relative path resolves against nothing, a remote URL is
    blocked by default in most mail clients, and this project's repository is
    private so a raw GitHub link would 404 for everyone. Only an embedded
    image survives all three."""
    html = rp.render_html(model(rp, gw))
    assert 'src="data:image/png;base64,' in html
    assert rp.logo_data_uri().startswith("data:image/png;base64,")


def test_the_page_pulls_no_subresource_over_the_network(rp, gw):
    """Links the reader can CLICK are fine. An asset the page needs in order
    to RENDER is not: it would be missing on a TV browser with no route to the
    internet, and stripped or blocked in mail."""
    html = rp.render_html(model(rp, gw))
    external = [u for u in re.findall(r'src="([^"]+)"', html)
                if not u.startswith("data:")]
    assert not external, external
    assert "<link" not in html
    assert "url(" not in html          # no CSS-referenced font or image


def test_a_missing_logo_costs_the_image_and_nothing_else(rp, gw, monkeypatch):
    """render_html has NO safety net: write_report catches OSError only, so
    anything else escapes to run(). A logo that cannot be read must degrade to
    no header image, never to a failed report."""
    monkeypatch.setattr(rp, "LOGO_FILE", "does-not-exist.png")
    monkeypatch.setattr(rp, "_logo_cache", [])
    assert rp.logo_data_uri() == ""
    html = rp.render_html(model(rp, gw))          # must not raise
    assert "<img" not in html, "an empty data URI must render no img at all"
    assert "Dustarr" in html


def test_the_footer_links_to_the_repository_and_the_issue_tracker(rp, gw):
    html = rp.render_html(model(rp, gw))
    assert f'href="{rp.REPO_URL}"' in html
    assert f'href="{rp.ISSUES_URL}"' in html


def test_the_report_issue_url_matches_the_plugins_own(rp):
    """reports.py deliberately does NOT import plugin.py (that is the
    direction the loader depends on), so the string is duplicated. Bind the
    two together here or they drift apart silently."""
    import sys
    plugin = sys.modules["dustarr_under_test.plugin"]
    assert rp.ISSUES_URL == plugin.ISSUES_URL
    assert rp.REPO_URL == plugin.ISSUES_URL.rsplit("/", 1)[0]


def test_the_cold_window_floor_matches_the_settings_floor():
    """reports.py deliberately does NOT import plugin.py (see the test above),
    so MIN_COLD_WINDOW_DAYS is duplicated as the lower bound of
    _NUMERIC_FLOORS["recent_window_days"]. Both comments say the two must
    agree: a cold window shorter than about a week can name a channel that
    is currently being watched, because the collector records a watch only
    when the session ends. Bind the two numbers together here or relaxing
    one silently reopens that defect."""
    import sys
    rp = sys.modules["dustarr_under_test.reports"]
    plugin = sys.modules["dustarr_under_test.plugin"]
    floor, _high = plugin._NUMERIC_FLOORS["recent_window_days"]
    assert rp.MIN_COLD_WINDOW_DAYS == floor


def test_section_glyphs_are_decoration_and_never_the_only_signal(rp, gw):
    """Same rule the palette follows. A client with no emoji font shows a box
    or nothing, so the coloured dot and the words have to carry the meaning on
    their own."""
    html = rp.render_html(model(rp, gw, n=8, watched=6))
    for summary in re.findall(r"<summary>(.*?)</summary>", html, re.DOTALL):
        for glyph in rp._SECTION_GLYPH.values():
            if glyph and glyph in summary:
                assert f'aria-hidden="true">{glyph}' in summary, summary
        # the dot is the non-emoji carrier and must be on every heading
        assert 'class="dot ' in summary


def test_every_glyph_is_keyed_on_a_dot_class_that_exists_in_the_css(rp):
    """Keying on the dot class rather than the title is what stops a section
    getting a glyph that disagrees with its colour. A typo in the key would
    silently render no glyph at all."""
    for dot_class in rp._SECTION_GLYPH:
        assert f".{dot_class} {{" in rp._CSS, dot_class


def test_a_non_numeric_cell_still_carries_a_numeric_sort_key(rp, gw):
    """One text cell in a numeric column makes the whole column sort as text
    (9 above 10), because the sort script needs BOTH cells to parse."""
    entries = [{"name": "A", "days_since_watched": "never",
                "days_since_watched_sort": rp.NEVER_SORT}]
    html = rp._table(entries)
    assert f"data-v='{rp.NEVER_SORT}'" in html
    assert ">never<" in html


def test_the_days_since_column_sorts_numerically_including_never_cells(rp, gw):
    """No browser is available in this environment, so clicking the column
    heading cannot be driven directly. The sort script (_SORT_JS) only takes
    its numeric path when BOTH compared cells' `data-v` parse via
    parseFloat() -- otherwise it falls back to text comparison, which would
    put "9" above "10". So the property that actually needs proving is that
    every cell in the "Days since" column, including the ones whose visible
    text reads "never", carries a `data-v` attribute that parses as a float.

    Uses a model with both watched (numeric days-since) and never-watched
    ("never" display, NEVER_SORT data-v) channels, so both cell shapes are
    exercised in the same rendered page."""
    days_since_idx = next(i for i, (key, _) in enumerate(rp._COLUMNS)
                          if key == "days_since_watched")
    html = rp.render_html(model(rp, gw, n=6, watched=2))

    checked = 0
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        cells = re.findall(r"<td[^>]*data-v='([^']*)'[^>]*>", row)
        # Only rows shaped like the per-channel table (one cell per
        # _COLUMNS entry) are in scope -- the per-group rollup table above
        # each section also uses <td data-v=...> cells but has a different
        # column count and is not the column under test.
        if len(cells) != len(rp._COLUMNS):
            continue
        raw = cells[days_since_idx]
        try:
            float(raw)
        except ValueError:
            pytest.fail(f"days-since data-v {raw!r} does not parse as a float")
        checked += 1
    assert checked > 0, "no per-channel table rows were found to check"


REPORT_SIZE_CEILING = 850_000


def test_a_full_size_report_stays_under_the_attachment_ceiling(rp, gw):
    """The report is emailed as an attachment against a 1 MiB cap. Measured at
    660,091 bytes for this shape (last_watched aged past the default 30 day
    cold window, so the "Channels going cold" section is populated rather
    than empty)."""
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=f"Channel number {i}",
                          group=f"Group {i % 13}", auto_created=False,
                          created_at=NOW - 300 * 86400, proxying=True)
            for i in range(1440)]
    # Aged in DAYS, not hours: at i * 3600s (an hour per channel) every
    # watched channel fell within 6.3 days of NOW, well inside the default
    # 30 day cold window, so "Channels going cold" rendered empty in the
    # very test meant to bound its size. i * 86400s pushes most channels
    # (i >= 31) past the window instead.
    channels = {f"u{i}": {"watch_count": 3, "watch_seconds": 7200.0,
                          "tune_count": 4, "last_watched": NOW - i * 86400,
                          "last_tuned": NOW - i * 86400,
                          "first_seen": NOW - 250 * 86400}
                for i in range(151)}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 200 * 86400, "coverage": {}}}
    model = rp.build_model(rows, usage, dict(SETTINGS, top_n=20), NOW)
    # The guard is only meaningful if the section it claims to bound actually
    # has rows in it.
    assert model["cold_abandoned"], "the cold section must not be empty here"
    html = rp.render_html(model)
    assert len(html.encode("utf-8")) < REPORT_SIZE_CEILING


def test_the_csv_carries_the_recency_columns(rp, gw):
    csv_text = rp.render_csv(model(rp, gw))
    header = next(line for line in csv_text.splitlines()
                  if not line.startswith("#"))
    assert "days_since_watched" in header
    assert "avg_session_minutes" in header


def test_masthead_renders_the_report_number_when_one_is_supplied(rp, gw):
    """The running total of published reports is surfaced ON the report, in its
    masthead, because that is the only place the number is both available and
    meaningful. It cannot be a README badge: the count lives in a file on the
    operator's own box (`/data/dustarr/report_count.json`) and this plugin never
    makes a network call, so nothing can publish it outward.
    """
    m = model(rp, gw)
    m["report_number"] = 42
    html = rp.render_html(m)
    assert "Report #42" in html
    assert 'class="chip chip-count"' in html


def test_masthead_omits_the_report_number_when_it_is_absent_or_zero(rp, gw):
    """A counter must never invent activity. An unreadable or missing count
    file degrades to 0 (`plugin.read_report_count`), and a "Report #0" chip
    would claim a report that does not exist, so no chip renders at all --
    the same degrade-to-nothing rule the logo follows.
    """
    for value in (None, 0, "", "not a number", -3):
        m = model(rp, gw)
        if value is not None:
            m["report_number"] = value
        html = rp.render_html(m)
        # Assert on the RENDERED chip, not on the bare class name: the
        # `.chip-count` CSS rule is in the stylesheet on every page whether the
        # chip renders or not, so a substring test for "chip-count" alone
        # passes only by accident and would never fail.
        assert '<span class="chip chip-count">' not in html, repr(value)
        assert "Report #" not in html, repr(value)


def test_report_number_chip_escapes_its_value(rp, gw):
    """`render_html` is one large f-string with no safety net around it, so any
    value reaching it must be coerced, not trusted. A string of digits is
    accepted (it is a number by any reasonable reading); anything else is
    dropped by the test above. This pins the coercion rather than the escape.
    """
    m = model(rp, gw)
    m["report_number"] = "17"
    assert "Report #17" in rp.render_html(m)
