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


def test_summary_for_webhook_is_allowlisted(rp, gw):
    model = rp.build_model(rows(gw, 10), watched(6), SETTINGS, NOW)
    summary = rp.summary_for_webhook(model, "http://x/report.html")
    assert set(summary) <= {"tracked_days", "coverage", "total_channels",
                            "never_watched", "tuned_never_qualified", "top",
                            "report_url", "alerts"}
    assert isinstance(summary["never_watched"], int)   # a COUNT, not the list
    assert summary["report_url"] == "http://x/report.html"
