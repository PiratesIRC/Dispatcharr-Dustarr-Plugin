import time as _time

import pytest
from conftest import load_plugin

load_plugin()


@pytest.fixture()
def rp():
    import sys
    return sys.modules["metricsarr_under_test.reports"]


@pytest.fixture()
def gw():
    import sys
    return sys.modules["metricsarr_under_test.gateway"]


NOW = 1_700_000_000.0
SETTINGS = {"exclude_groups": "", "exclude_name_regex": "",
            "exclude_auto_created": False, "top_n": 3,
            "unused_threshold_days": 30, "never_watched_ceiling": 0.99,
            "poll_interval_s": 15.0, "client_gap_grace_s": 90.0}


def rows(gw, n, **kw):
    out = []
    for i in range(n):
        base = {"id": i, "uuid": f"u{i}", "name": f"CH{i}", "group": "US: Movies",
                "auto_created": False, "created_at": NOW - 90 * 86400,
                "proxying": True}
        base.update(kw)
        out.append(gw.ChannelRow(**base))
    return out


def watched(uuid_count, now=NOW):
    channels = {}
    for i in range(uuid_count):
        channels[f"u{i}"] = {"watch_count": 5 - (i % 3), "watch_seconds": 3600.0 * (i + 1),
                             "tune_count": 6, "last_watched": now - 3600,
                             "last_tuned": now - 3600, "first_seen": now - 80 * 86400}
    return {"channels": channels,
            "meta": {"stats_since": now - 40 * 86400, "coverage": {}}}


def _full_coverage(now, hours=24, poll_interval_s=15.0):
    """A synthetic meta.coverage dict that scores 100% under gates.coverage_fraction."""
    needed_ticks = int(3600.0 / poll_interval_s) + 5
    coverage = {}
    for i in range(hours):
        ts = now - i * 3600
        key = _time.strftime("%Y-%m-%dT%H", _time.gmtime(ts))
        coverage[key] = {"ticks": needed_ticks, "max_gap_s": 10.0}
    return coverage


def test_absent_from_usage_means_never_watched(rp, gw):
    """THE invariant: 1000 ORM channels + 3 usage rows => 997 never-watched.

    The collector only records channels it OBSERVES; the never-watched channels --
    the plugin's whole purpose -- exist only in the ORM (consistency #1).
    """
    model = rp.build_model(rows(gw, 1000), watched(3), SETTINGS, NOW)
    assert model["total_channels"] == 1000
    assert len(model["never_watched"]) == 997
    assert model["counts"]["never_watched"] == 997


def test_never_watched_entries_carry_a_reason_and_no_crash_on_absent_record(rp, gw):
    model = rp.build_model(rows(gw, 5), {"channels": {}, "meta": {}}, SETTINGS, NOW)
    entry = model["never_watched"][0]
    assert entry["reason"] == "never_watched"
    assert entry["watch_count"] == 0
    assert entry["last_watched"] is None


def test_tuned_but_never_qualified_is_its_own_bucket(rp, gw):
    """These channels are BROKEN, not unused: the user tried to watch them and
    gave up inside min_watch_seconds (red-team #12). Conflating them with unused
    channels is how a metrics tool disables the channels you fight to watch."""
    usage = {"channels": {"u1": {"watch_count": 0, "watch_seconds": 0.0,
                                 "tune_count": 4, "last_watched": None,
                                 "last_tuned": NOW - 7200,
                                 "first_seen": NOW - 80 * 86400}},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
    assert [c["uuid"] for c in model["tuned_never_qualified"]] == ["u1"]
    # And it must NOT also be listed as never-watched.
    assert "u1" not in [c["uuid"] for c in model["never_watched"]]
    assert model["tuned_never_qualified"][0]["reason"] == "tuned_never_qualified"


def test_most_and_least_used_are_ranked_and_capped_at_top_n(rp, gw):
    model = rp.build_model(rows(gw, 10), watched(10), SETTINGS, NOW)
    assert len(model["most_used"]) == 3          # top_n
    assert len(model["least_used"]) == 3
    counts = [c["watch_count"] for c in model["most_used"]]
    assert counts == sorted(counts, reverse=True)


def test_hours_are_derived_from_watch_seconds(rp, gw):
    model = rp.build_model(rows(gw, 1), watched(1), SETTINGS, NOW)
    assert model["most_used"][0]["hours"] == pytest.approx(1.0)


def test_excluded_channels_are_reported_not_silently_dropped(rp, gw):
    settings = dict(SETTINGS, exclude_auto_created=True)
    channels = rows(gw, 3) + rows(gw, 2, auto_created=True)
    for i, row in enumerate(channels[3:], start=3):
        channels[i] = row._replace(uuid=f"auto{i}")
    model = rp.build_model(channels, watched(0), settings, NOW)
    assert len(model["excluded"]) == 2
    assert all(c["reason"] == "excluded:auto_created" for c in model["excluded"])
    # Excluded channels are NOT counted as never-watched.
    assert model["counts"]["never_watched"] == 3


def test_unobservable_channels_are_never_counted_as_unused(rp, gw):
    channels = rows(gw, 2) + [rows(gw, 1, proxying=False)[0]._replace(uuid="redir")]
    model = rp.build_model(channels, watched(0), SETTINGS, NOW)
    assert [c["uuid"] for c in model["unobservable"]] == ["redir"]
    assert "redir" not in [c["uuid"] for c in model["never_watched"]]


def test_group_rollup_counts_never_watched_per_group(rp, gw):
    channels = rows(gw, 4) + [r._replace(uuid=f"uk{i}", group="UK: All")
                              for i, r in enumerate(rows(gw, 6))]
    model = rp.build_model(channels, watched(2), SETTINGS, NOW)
    rollup = {g["group"]: g for g in model["group_rollup"]}
    assert rollup["UK: All"]["never"] == 6
    assert rollup["UK: All"]["total"] == 6
    assert rollup["US: Movies"]["never"] == 2      # u0, u1 were watched


def test_too_new_channels_are_flagged(rp, gw):
    """A channel created 3 days ago cannot be 30 days unused; the report must say
    so rather than listing it as dead weight (consistency #1)."""
    fresh = rows(gw, 1, created_at=NOW - 3 * 86400)
    model = rp.build_model(fresh, watched(0), SETTINGS, NOW)
    assert model["never_watched"][0]["reason"] == "too_new"
    assert model["never_watched"][0]["age_days"] == pytest.approx(3.0, abs=0.1)


def test_channel_with_no_created_at_does_not_crash(rp, gw):
    model = rp.build_model(rows(gw, 1, created_at=None), watched(0), SETTINGS, NOW)
    assert model["total_channels"] == 1


def test_gate_result_is_attached_to_the_model(rp, gw):
    model = rp.build_model(rows(gw, 10), watched(0), SETTINGS, NOW)
    assert model["gate"]["ok"] is False            # zero watches => blind sensor
    assert model["gate"]["alerts"]


def test_summary_for_notify_is_allowlisted(rp, gw):
    model = rp.build_model(rows(gw, 10), watched(6), SETTINGS, NOW)
    summary = rp.summary_for_notify(model, "http://x/report.html")
    assert set(summary) <= {"tracked_days", "coverage", "total_channels",
                            "never_watched", "tuned_never_qualified", "top",
                            "report_url", "alerts"}
    assert isinstance(summary["never_watched"], int)   # a COUNT, not the list
    assert summary["report_url"] == "http://x/report.html"
    # I2: the per-entry shape of `top` is unprotected by the top-level check
    # above -- lock it down explicitly so uuid/group/last_watched/tune_count/
    # reason (which could carry provider-adjacent identifiers) can never leak
    # into the notify payload via a careless refactor of the comprehension.
    assert summary["top"], "expected at least one top entry for this fixture"
    assert set(summary["top"][0]) == {"name", "watch_count", "hours"}


def test_unobservable_alert_fires_when_lineup_mostly_unobservable(rp, gw):
    """C1: gates.unobservable_alert() must be wired into build_model's gate.

    Reviewer's repro: 1000 channels, 950 non-proxying (unobservable), the 50
    observable ones healthily watched with full coverage. Every other gate
    passes on this fixture -- only the unobservable-fraction gate should trip.
    """
    obs = rows(gw, 50, proxying=True)                              # uuid u0..u49
    unobs = [r._replace(uuid=f"x{i}") for i, r in
             enumerate(rows(gw, 950, proxying=False))]
    usage = watched(50, now=NOW)
    usage["meta"]["coverage"] = _full_coverage(NOW)
    settings = dict(SETTINGS, unused_threshold_days=1)   # keep the coverage window small

    model = rp.build_model(obs + unobs, usage, settings, NOW)

    assert model["counts"]["unobservable"] == 950
    assert model["gate"]["ok"] is False
    assert any("unobservable" in a for a in model["gate"]["alerts"])


def test_unobservable_alert_absent_when_lineup_is_mostly_observable(rp, gw):
    """Sanity companion: with observability healthy, no unobservable alert fires."""
    obs = rows(gw, 50, proxying=True)
    usage = watched(50, now=NOW)
    usage["meta"]["coverage"] = _full_coverage(NOW)
    settings = dict(SETTINGS, unused_threshold_days=1)
    model = rp.build_model(obs, usage, settings, NOW)
    assert not any("unobservable" in a for a in model["gate"]["alerts"])


def test_least_used_is_disjoint_from_most_used_and_ascending(rp, gw):
    """I1/I3: the reversed-slice defect made least_used overlap most_used on
    short lists (e.g. 5 watched channels, top_n=3 -> both lists contained
    u2). Assert disjointness AND ascending order so a regression to either
    `list(reversed(used))[:top_n]` (overlap) or `used[:top_n]` (byte-identical
    to most_used) is caught."""
    settings = dict(SETTINGS, top_n=3)
    model = rp.build_model(rows(gw, 5), watched(5), settings, NOW)

    most_uuids = {c["uuid"] for c in model["most_used"]}
    least_uuids = {c["uuid"] for c in model["least_used"]}
    assert len(model["least_used"]) == 2   # 5 watched - 3 most_used, capped at top_n
    assert not (most_uuids & least_uuids), "least_used overlaps most_used"

    counts = [c["watch_count"] for c in model["least_used"]]
    assert counts == sorted(counts)        # ascending -- the bottom of the list


def test_malformed_usage_records_do_not_crash(rp, gw):
    """I4: usage.json is untrusted file input -- storage.load() validates only
    the top-level shape is a dict, never per-record field types. None of these
    three malformed shapes may raise."""
    base_meta = {"stats_since": NOW - 40 * 86400, "coverage": {}}
    for bad_record in (
        {"watch_count": None},
        {"watch_count": "3"},
        {"watch_seconds": "abc"},
    ):
        usage = {"channels": {"u0": bad_record}, "meta": dict(base_meta)}
        model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
        assert model["total_channels"] == 3


def test_malformed_watch_count_string_is_coerced_and_ranks(rp, gw):
    usage = {"channels": {"u0": {"watch_count": "3", "watch_seconds": 60.0}},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
    assert model["most_used"][0]["uuid"] == "u0"
    assert model["most_used"][0]["watch_count"] == 3


def test_malformed_watch_seconds_string_degrades_to_zero_hours(rp, gw):
    usage = {"channels": {"u0": {"watch_count": 1, "watch_seconds": "abc"}},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
    assert model["most_used"][0]["hours"] == 0.0


def test_too_new_is_a_peer_count_excluded_from_never_watched(rp, gw):
    """I5: too_new entries stay inside the never_watched LIST (Task 9 renders
    them there with their own reason) but must not inflate the headline
    never_watched COUNT or the notify payload."""
    fresh = rows(gw, 5, created_at=NOW - 3 * 86400)                # too_new
    stale = [r._replace(uuid=f"s{i}") for i, r in
             enumerate(rows(gw, 2, created_at=NOW - 90 * 86400))]  # true never_watched
    model = rp.build_model(fresh + stale, watched(0), SETTINGS, NOW)

    assert len(model["never_watched"]) == 7        # both kinds stay in the list
    assert model["counts"]["too_new"] == 5
    assert model["counts"]["never_watched"] == 2    # only the genuinely stale ones
    assert (model["counts"]["never_watched"] + model["counts"]["too_new"]
            + model["counts"]["tuned_never_qualified"] + model["counts"]["watched"]
            + model["counts"]["excluded"] + model["counts"]["unobservable"]) == 7

    summary = rp.summary_for_notify(model, "http://x/report.html")
    assert summary["never_watched"] == 2


def test_too_new_channels_do_not_trip_the_never_watched_ceiling_gate(rp, gw):
    """A fresh bulk M3U import (many `too_new` channels) must not itself trip
    the >60% never-watched ceiling gate -- only genuine `never_watched` rows
    may count toward that fraction."""
    settings = dict(SETTINGS, never_watched_ceiling=0.60, unused_threshold_days=30)
    fresh = [r._replace(uuid=f"f{i}") for i, r in
             enumerate(rows(gw, 80, created_at=NOW - 3 * 86400))]   # too_new
    watched_rows = rows(gw, 20, created_at=NOW - 90 * 86400)        # uuid u0..u19
    model = rp.build_model(fresh + watched_rows, watched(20, now=NOW), settings, NOW)
    # 80/100 = 80% would trip the 60% ceiling if too_new counted; it must not.
    assert not any("ceiling" in a for a in model["gate"]["alerts"])


def test_unobservable_channel_with_real_watch_history_still_ranks(rp, gw):
    """M1: a channel's stream profile can flip to non-proxying AFTER it was
    watched. Real usage history must not vanish into `unobservable`."""
    channel = rows(gw, 1, proxying=False)[0]._replace(uuid="was-watched")
    usage = {"channels": {"was-watched": {
                 "watch_count": 4, "watch_seconds": 3600.0, "tune_count": 5,
                 "last_watched": NOW - 3600, "last_tuned": NOW - 3600,
                 "first_seen": NOW - 80 * 86400}},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    model = rp.build_model([channel], usage, SETTINGS, NOW)
    assert model["counts"]["unobservable"] == 0
    assert [c["uuid"] for c in model["most_used"]] == ["was-watched"]


def test_group_rollup_total_is_the_true_orm_group_total(rp, gw):
    """M2: group_rollup['total'] is the group's true ORM channel count,
    including excluded rows -- not just the rows that landed in never/used/
    tuned_only."""
    settings = dict(SETTINGS, exclude_auto_created=True)
    group_rows = rows(gw, 3)                                        # eligible
    excluded_rows = [r._replace(uuid=f"auto{i}", auto_created=True)
                     for i, r in enumerate(rows(gw, 2))]
    model = rp.build_model(group_rows + excluded_rows, watched(0), settings, NOW)
    rollup = {g["group"]: g for g in model["group_rollup"]}
    assert rollup["US: Movies"]["total"] == 5      # 3 eligible + 2 excluded


def test_top_n_zero_is_respected_not_defaulted(rp, gw):
    """M3: top_n=0 is a valid, intentional setting -- must not silently fall
    back to the default of 20."""
    settings = dict(SETTINGS, top_n=0)
    model = rp.build_model(rows(gw, 5), watched(5), settings, NOW)
    assert model["most_used"] == []
    assert model["least_used"] == []


def test_malformed_meta_stats_since_does_not_crash(rp, gw):
    """R1: meta.stats_since is untrusted too -- a corrupt value used to raise
    ValueError from float(stats_since) in both this module and gates.evaluate()."""
    usage = {"channels": {}, "meta": {"stats_since": "abc", "coverage": {}}}
    model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
    assert model["total_channels"] == 3
    assert model["stats_since"] is None


def test_malformed_meta_coverage_bucket_does_not_crash(rp, gw):
    """R1: a corrupt coverage bucket used to raise TypeError out of
    gates.coverage_fraction (None/str compared against a float threshold).
    The bucket key must fall inside the window gates.evaluate() actually
    scans (anchored to NOW), or coverage_fraction never looks it up."""
    bad_key = _time.strftime("%Y-%m-%dT%H", _time.gmtime(NOW))
    usage = {"channels": {}, "meta": {
        "stats_since": NOW - 40 * 86400,
        "coverage": {bad_key: {"ticks": None, "max_gap_s": "abc"}}}}
    model = rp.build_model(rows(gw, 3), usage, SETTINGS, NOW)
    assert model["total_channels"] == 3


def test_top_n_negative_is_clamped_to_zero(rp, gw):
    """R3: top_n=-1 must not silently become `used[:-1]` (dropping the last
    entry) -- clamp to >= 0, distinct from the explicit top_n=0 case (M3)."""
    settings = dict(SETTINGS, top_n=-1)
    model = rp.build_model(rows(gw, 5), watched(5), settings, NOW)
    assert model["most_used"] == []
    assert model["least_used"] == []


def test_negative_watch_count_is_not_bucketed_as_watched(rp, gw):
    """M5: a negative watch_count (corrupt data) must not be truthy-bucketed
    into `used` with negative hours."""
    usage = {"channels": {"u0": {"watch_count": -3, "watch_seconds": -100.0,
                                 "tune_count": 0, "last_watched": None,
                                 "last_tuned": None, "first_seen": None}},
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    model = rp.build_model(rows(gw, 1), usage, SETTINGS, NOW)
    assert model["counts"]["watched"] == 0
    assert model["most_used"] == []
