"""Age-based cleanup of the dated report archives in /config/dustarr.

Two file streams live in that directory: `report-<stamp>.csv` and
`report-<stamp>.html`, alongside the live `report.html`, which is not dated and
must never be swept. Both dated streams are already bounded by COUNT
(`_prune_archives` keeps `ARCHIVE_KEEP`); this module covers the separate
age rule, which deletes a file once it is older than a configured number of
days.

Every test that exercises the age rule or the off-by-default rule plants
SEVERAL old files. With a single file the "at least one always survives" rule
keeps it regardless, so such a test passes even when the guard it names has
been deleted, and proves nothing.
"""

import os

import pytest
from conftest import NOW, load_plugin, model

load_plugin()

DAY = 86400.0


@pytest.fixture()
def plugin():
    """The plugin module, with the collector crash-loop budget reset.

    Same reason as the fixture in tests/test_plugin_actions.py: that budget is
    a module global, so a test that leaves it spent makes an unrelated later
    test fail.
    """
    module = load_plugin()
    module._restart_times.clear()
    return module

# Real filenames measured in the shared /data/exports directory on the live
# system. They are here so a sweep that matched on suffix alone, or on a bare
# glob, is caught by name rather than in production.
OTHER_PLUGINS = [
    "stream_mapparr_20260901-030000.csv",
    "epg_janitor_20260901-030000.csv",
    "event_channel_managarr_20260901-030000.csv",
    "lineuparr_20260901-030000.csv",
    "iptv_checker_results_20260901-030000.csv",
    "channel_mapparr_20260901-030000.csv",
]


def aged(name, days):
    """One (filename, modification time) pair, that many days before NOW."""
    return (name, NOW - days * DAY)


def five_old_csvs(days=90):
    """Five dated CSV exports, all far older than any plausible retention.

    Five, not one: the survivor rule keeps a lone file whatever the age rule
    says, so a one-file fixture cannot tell a working age rule from a deleted
    one.
    """
    return [aged(f"report-2026080{n}-030000.csv", days + n) for n in range(1, 6)]


# ---------------------------------------------------------------------------
# Which files belong to this plugin
# ---------------------------------------------------------------------------

def test_other_plugins_exports_are_never_deleted(rp):
    """The prefix check is the one that protects other projects' data.

    /data/exports is shared by at least six plugins, and this plugin's report
    directory is only private by convention. Selecting on the .csv suffix alone
    would delete over a hundred other files on a first run with a seven day
    rule.
    """
    entries = [aged(name, 90) for name in OTHER_PLUGINS] + five_old_csvs()

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    for name in OTHER_PLUGINS:
        assert name not in doomed
    assert doomed, "the plugin's own old exports should still be swept"


def test_the_html_archives_are_left_alone_by_the_csv_sweep(rp):
    """The suffix check keeps the two dated streams apart.

    Both are named `report-<stamp>`, so prefix alone cannot separate them and a
    CSV sweep would take the HTML archives with it.
    """
    html = [aged(f"report-2026080{n}-030000.html", 90) for n in range(1, 6)]
    entries = html + five_old_csvs()

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    for name, _ in html:
        assert name not in doomed
    assert doomed


def test_the_live_report_is_never_deleted(rp):
    """`report.html` is the file the operator opens. It carries no stamp, so
    the `report-` prefix must exclude it however old it is."""
    entries = [aged("report.html", 900)] + [
        aged(f"report-2026080{n}-030000.html", 90) for n in range(1, 6)
    ]

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".html")

    assert "report.html" not in doomed


# ---------------------------------------------------------------------------
# Off unless configured
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("configured", [0, 0.0, "0", -1, -365, "", "   ", None,
                                        "forever", "7 days", [], {}, float("nan")])
def test_nothing_is_deleted_unless_a_positive_number_of_days_is_configured(rp, configured):
    """Nobody loses a file merely by upgrading to a version that has this
    setting. Zero, negative, blank, absent and unparseable all mean keep
    everything.

    Five old files, so the survivor rule cannot make a broken check look
    correct.
    """
    assert rp.aged_files_to_delete(five_old_csvs(), configured, NOW, "report-", ".csv") == []


# ---------------------------------------------------------------------------
# The age arithmetic
# ---------------------------------------------------------------------------

def test_exactly_n_days_old_is_not_older_than_n_days(rp):
    """The comparison is strict. Five files sitting exactly on the boundary
    stay; an inclusive comparison would delete four of them."""
    entries = [aged(f"report-2026080{n}-030000.csv", 7) for n in range(1, 6)]

    assert rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv") == []


def test_a_file_one_second_past_the_boundary_is_deleted(rp):
    # A fresh file is present so the boundary file is not the newest, and so
    # cannot be kept by the survivor rule instead of being judged on its age.
    entries = [("report-20260801-030000.csv", NOW - 7 * DAY - 1),
               aged("report-20260905-030000.csv", 1)] + five_old_csvs()

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    assert "report-20260801-030000.csv" in doomed


def test_files_younger_than_the_retention_are_kept(rp):
    entries = [aged(f"report-2026090{n}-030000.csv", n) for n in range(1, 6)]

    assert rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv") == []


# ---------------------------------------------------------------------------
# At least one file always survives
# ---------------------------------------------------------------------------

def test_the_newest_file_survives_when_every_file_is_old(rp):
    """A small retention number must not be able to empty the directory."""
    entries = five_old_csvs()
    newest = max(entries, key=lambda pair: pair[1])[0]

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    assert newest not in doomed
    assert len(doomed) == len(entries) - 1


def test_the_file_just_written_is_never_deleted_however_old_it_looks(rp):
    """The protected name is excluded whatever the age arithmetic says, and it
    takes the survivor slot, so the newest of the others is still swept.

    The protected file is deliberately the OLDEST here: if the code merely kept
    the newest file, this test would still see the protected one deleted.
    """
    entries = five_old_csvs() + [aged("report-20260101-030000.csv", 900)]
    newest_other = max(five_old_csvs(), key=lambda pair: pair[1])[0]

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv",
                                     protect="report-20260101-030000.csv")

    assert "report-20260101-030000.csv" not in doomed
    assert newest_other in doomed


# ---------------------------------------------------------------------------
# A modification time that is not a number
# ---------------------------------------------------------------------------

def test_a_not_a_number_modification_time_never_becomes_the_survivor(rp):
    """NaN compares False against everything, so an unguarded `max` hands it
    the survivor slot and every real file is deleted instead of one being kept.

    It is listed first because that is the order in which an unguarded `max`
    lets it win, which is the failure this pins.
    """
    entries = [("report-20260815-030000.csv", float("nan"))] + five_old_csvs()
    newest = max(five_old_csvs(), key=lambda pair: pair[1])[0]

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    assert newest not in doomed, "a real file, not the NaN entry, must survive"
    assert "report-20260815-030000.csv" not in doomed


def test_a_modification_time_that_is_not_a_number_at_all_is_skipped(rp):
    """A string or None cannot even be compared against a float without
    raising, so it is dropped rather than carried into the arithmetic."""
    entries = [("report-20260815-030000.csv", "corrupt"),
               ("report-20260816-030000.csv", None)] + five_old_csvs()

    doomed = rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv")

    assert "report-20260815-030000.csv" not in doomed
    assert "report-20260816-030000.csv" not in doomed
    assert len(doomed) == 4


def test_no_matching_files_deletes_nothing(rp):
    entries = [aged(name, 900) for name in OTHER_PLUGINS]

    assert rp.aged_files_to_delete(entries, 7, NOW, "report-", ".csv") == []


# ---------------------------------------------------------------------------
# The filesystem wrapper
# ---------------------------------------------------------------------------

def plant(dirpath, names_and_days):
    for name, days in names_and_days:
        path = os.path.join(str(dirpath), name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        stamp = NOW - days * DAY
        os.utime(path, (stamp, stamp))


def test_prune_aged_deletes_the_old_files_and_returns_how_many(rp, tmp_path):
    plant(tmp_path, [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)])
    plant(tmp_path, [(name, 90) for name in OTHER_PLUGINS])

    removed = rp.prune_aged(str(tmp_path), "report-", ".csv", 7, NOW)

    assert removed == 4
    survivors = sorted(os.listdir(str(tmp_path)))
    for name in OTHER_PLUGINS:
        assert name in survivors


def test_prune_aged_never_raises_when_a_delete_fails(rp, tmp_path, monkeypatch):
    """This runs immediately after a successful export. A failure to tidy up
    must not turn that export into a reported error."""
    plant(tmp_path, [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)])

    def refuse(path):
        raise OSError("permission denied")

    monkeypatch.setattr(rp.os, "remove", refuse)

    assert rp.prune_aged(str(tmp_path), "report-", ".csv", 7, NOW) == 0
    assert len(os.listdir(str(tmp_path))) == 5


def test_prune_aged_never_raises_when_the_directory_is_missing(rp, tmp_path):
    assert rp.prune_aged(str(tmp_path / "gone"), "report-", ".csv", 7, NOW) == 0


def test_prune_aged_survives_a_file_vanishing_between_listing_and_stat(rp, tmp_path, monkeypatch):
    plant(tmp_path, [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)])
    real_getmtime = rp.os.path.getmtime

    def vanish(path):
        if path.endswith("report-20260801-030000.csv"):
            raise OSError("no such file")
        return real_getmtime(path)

    monkeypatch.setattr(rp.os.path, "getmtime", vanish)

    assert rp.prune_aged(str(tmp_path), "report-", ".csv", 7, NOW) == 3


# ---------------------------------------------------------------------------
# Wired into the export
# ---------------------------------------------------------------------------

def test_write_report_prunes_old_csv_exports(rp, gw, tmp_path):
    plant(tmp_path, [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)])

    out = rp.write_report(model(rp, gw), str(tmp_path), str(tmp_path), NOW,
                          retention_days=7)

    # Every planted file is gone. The file just written holds the survivor
    # slot, so the newest planted one is not additionally spared.
    left = sorted(n for n in os.listdir(str(tmp_path)) if n.endswith(".csv"))
    assert left == [os.path.basename(out["csv_path"])]


def test_write_report_prunes_old_html_archives(rp, gw, tmp_path):
    plant(tmp_path, [(f"report-2026080{n}-030000.html", 90 + n) for n in range(1, 6)])

    out = rp.write_report(model(rp, gw), str(tmp_path), str(tmp_path), NOW,
                          retention_days=7)

    left = sorted(n for n in os.listdir(str(tmp_path)) if n.endswith(".html"))
    assert "report.html" in left, "the live report is not a dated archive"
    assert left == sorted(["report.html", os.path.basename(out["archive_path"])])


def test_write_report_keeps_everything_when_retention_is_off(rp, gw, tmp_path):
    """The default is off, so an installation that never asked for this keeps
    every file. Five planted files, so the survivor rule cannot fake a pass."""
    planted = [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)]
    plant(tmp_path, planted)

    rp.write_report(model(rp, gw), str(tmp_path), str(tmp_path), NOW)

    left = os.listdir(str(tmp_path))
    for name, _ in planted:
        assert name in left


def test_write_report_never_deletes_the_files_it_just_wrote(rp, gw, tmp_path):
    plant(tmp_path, [(f"report-2026080{n}-030000.csv", 90 + n) for n in range(1, 6)])

    out = rp.write_report(model(rp, gw), str(tmp_path), str(tmp_path), NOW,
                          retention_days=1)

    assert os.path.exists(out["html_path"])
    assert os.path.exists(out["csv_path"])
    assert os.path.exists(out["archive_path"])


# ---------------------------------------------------------------------------
# The setting, and its journey from the settings dict to the sweep
# ---------------------------------------------------------------------------

def field(plugin, fid):
    return next(f for f in plugin.FIELDS if f["id"] == fid)


def test_the_retention_setting_exists_and_is_off_by_default(plugin):
    spec = field(plugin, "report_retention_days")
    assert spec["type"] == "number"
    assert spec["default"] == 0


def test_the_retention_setting_is_coerced_to_a_whole_number_of_days(plugin):
    coerced = plugin.coerce_settings({"report_retention_days": "30"})
    assert coerced["report_retention_days"] == 30
    assert isinstance(coerced["report_retention_days"], int)


def test_an_unusable_retention_setting_falls_back_to_off(plugin):
    for bad in ("forever", None, float("nan"), -5, [], {}):
        coerced = plugin.coerce_settings({"report_retention_days": bad})
        assert coerced["report_retention_days"] == 0, bad


def test_the_retention_setting_does_not_respawn_the_collector(plugin):
    """It is a report-only setting. Hashing it into the collector fingerprint
    would restart the collector and forfeit every in-flight watch session."""
    base = plugin.coerce_settings({})
    changed = plugin.coerce_settings({"report_retention_days": 30})
    assert plugin._thresholds_fingerprint(base) == plugin._thresholds_fingerprint(changed)


def test_build_report_hands_the_retention_setting_to_the_writer(plugin, monkeypatch):
    """Without this the sweep never runs, however the setting is configured."""
    seen = {}

    def spy(model, report_dir, csv_dir, now, retention_days=0):
        seen["retention_days"] = retention_days
        return {"html_path": None, "csv_path": None, "archive_path": None}

    monkeypatch.setattr(plugin.reports, "write_report", spy)
    plugin._build_report({"report_retention_days": 30})

    assert seen["retention_days"] == 30


# ---------------------------------------------------------------------------
# The two pruning passes share one definition of "this file is ours"
# ---------------------------------------------------------------------------
# Found by mutation testing on 2026-09-05, after the startswith/endswith pair
# was factored out of both pruning passes into `_matching_files`. Removing the
# SUFFIX half of that pair failed no test at all, yet it is destructive: both
# dated streams live in one directory and share the `report-` prefix, so a
# prune of the HTML stream then matches the CSV files too. Measured with the
# suffix check removed: ten CSV exports dropped to four during an HTML prune.
#
# The count cap (`_prune_archives`) had no test for this before the refactor
# either; the check was simply inline. This closes that.


def test_pruning_the_html_stream_by_count_does_not_touch_the_csv_stream(rp, tmp_path):
    """Ten of each, not one of each: ARCHIVE_KEEP is 8, so with fewer than nine
    files of a kind the count cap deletes nothing and the test cannot fail."""
    for n in range(1, 11):
        for ext in ("html", "csv"):
            (tmp_path / f"report-202608{n:02d}-030000.{ext}").write_text("x")

    rp._prune_archives(str(tmp_path), "report-", ".html")

    left = os.listdir(str(tmp_path))
    assert len([f for f in left if f.endswith(".csv")]) == 10, \
        "the HTML prune deleted CSV exports"
    assert len([f for f in left if f.endswith(".html")]) == rp.ARCHIVE_KEEP


def test_pruning_the_csv_stream_by_count_does_not_touch_the_html_stream(rp, tmp_path):
    for n in range(1, 11):
        for ext in ("html", "csv"):
            (tmp_path / f"report-202608{n:02d}-030000.{ext}").write_text("x")

    rp._prune_archives(str(tmp_path), "report-", ".csv")

    left = os.listdir(str(tmp_path))
    assert len([f for f in left if f.endswith(".html")]) == 10, \
        "the CSV prune deleted HTML archives"
    assert len([f for f in left if f.endswith(".csv")]) == rp.ARCHIVE_KEEP


def test_neither_pruning_pass_touches_the_live_report(rp, tmp_path):
    """`report.html` carries no stamp, so the `report-` prefix must exclude it.
    It is the file the operator opens."""
    (tmp_path / "report.html").write_text("x")
    for n in range(1, 11):
        (tmp_path / f"report-202608{n:02d}-030000.html").write_text("x")

    rp._prune_archives(str(tmp_path), "report-", ".html")
    rp.prune_aged(str(tmp_path), "report-", ".html", 1, NOW + 3650 * DAY)

    assert (tmp_path / "report.html").exists()
