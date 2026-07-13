import time

import pytest
from conftest import load_pure


@pytest.fixture()
def gates():
    return load_pure("gates")


def buckets(now, hours, ticks, max_gap=15.0):
    out = {}
    for i in range(hours):
        key = time.strftime("%Y-%m-%dT%H", time.gmtime(now - i * 3600))
        out[key] = {"ticks": ticks, "max_gap_s": max_gap}
    return out


def test_full_coverage_reads_one(gates):
    now = 1_700_000_000.0
    cov = buckets(now, 24 * 7, ticks=240)          # 3600/15 = 240 ticks/hour
    frac = gates.coverage_fraction(cov, now, window_days=7, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0)
    assert frac == pytest.approx(1.0, abs=0.02)


def test_one_tick_per_hour_is_NOT_covered(gates):
    """A collector wedged 55 minutes of every hour scored 100% under rev 1's
    boolean buckets. Density is what matters (red-team #5)."""
    now = 1_700_000_000.0
    cov = buckets(now, 24 * 7, ticks=1, max_gap=3500.0)
    frac = gates.coverage_fraction(cov, now, window_days=7, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0)
    assert frac == 0.0


def test_a_gap_longer_than_the_grace_vetoes_the_hour(gates):
    """A whole session could have opened and closed inside the gap."""
    now = 1_700_000_000.0
    cov = buckets(now, 24 * 7, ticks=240, max_gap=200.0)   # > 90s grace
    frac = gates.coverage_fraction(cov, now, window_days=7, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0)
    assert frac == 0.0


def test_missing_buckets_reduce_coverage(gates):
    now = 1_700_000_000.0
    cov = buckets(now, 12, ticks=240)              # only 12 of 24 hours
    frac = gates.coverage_fraction(cov, now, window_days=1, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0)
    assert frac == pytest.approx(0.5, abs=0.05)


def test_density_alone_vetoes_the_hour(gates):
    """Half the expected ticks, but every gap inside the grace. The density check
    is the ONLY thing that can catch this -- test_one_tick_per_hour double-signals
    through max_gap and does not pin it."""
    now = 1_700_000_000.0
    cov = buckets(now, 24, ticks=120, max_gap=15.0)   # 120 of 240 expected
    assert gates.coverage_fraction(cov, now, window_days=1, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0) == 0.0


def test_window_is_the_RECENT_hours_not_just_any_hours(gates):
    """A keys()-walk that preserves the denominator would read the OLDEST hours
    once coverage is pruned past the window -- a collector dead for the last 10
    of 45 pruned days would wrongly score 1.0. Pins that the window anchors to
    `now`, not to whatever keys happen to exist."""
    now = 1_700_000_000.0
    cov = {}                                   # 35 perfect days, then dead for 10
    for i in range(24 * 35):
        key = time.strftime("%Y-%m-%dT%H", time.gmtime(now - (i + 24 * 10) * 3600))
        cov[key] = {"ticks": 240, "max_gap_s": 15.0}
    frac = gates.coverage_fraction(cov, now, window_days=30, poll_interval_s=15.0,
                                   client_gap_grace_s=90.0)
    assert frac == pytest.approx(20 / 30.0, abs=0.02)


def usage_with(now, watched_channels, stats_since=None, coverage=None):
    channels = {}
    for i in range(watched_channels):
        channels[f"u{i}"] = {"watch_count": 3, "watch_seconds": 3600.0,
                             "tune_count": 3, "last_watched": now - 3600,
                             "last_tuned": now - 3600, "first_seen": now - 86400}
    return {"channels": channels,
            "meta": {"stats_since": stats_since if stats_since is not None
                     else now - 40 * 86400,
                     "coverage": coverage if coverage is not None
                     else buckets(now, 24 * 45, ticks=240)}}


THRESHOLDS = {"poll_interval_s": 15.0, "client_gap_grace_s": 90.0,
              "unused_threshold_days": 30, "never_watched_ceiling": 0.60}


def test_healthy_dataset_passes(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40)
    result = gates.evaluate(usage, rows_total=1440, never_watched=800, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is True
    assert result["alerts"] == []


def test_zero_watches_in_7_days_means_the_sensor_is_blind(gates):
    """A present, parseable, coverage-complete usage.json full of zeros is the
    mass-casualty case: a household that watches TV produces watches (red-team #4)."""
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=0)
    result = gates.evaluate(usage, rows_total=1440, never_watched=1440, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("no qualified watches" in a for a in result["alerts"])


def test_stale_watches_outside_the_7_day_window_still_trip_the_gate(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=10)
    for rec in usage["channels"].values():
        rec["last_watched"] = now - 30 * 86400      # all watches are ancient
    result = gates.evaluate(usage, rows_total=1440, never_watched=1430, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False


def test_one_recent_watch_does_not_satisfy_the_blindness_gate(gates):
    """Reviewer's breach repro: 40 channels, 39 last watched 200 days ago, 1
    watched 6 days ago, coverage 100%, stats_since 400 days old. A non-empty
    `recent` list and a `distinct` count of 40 (which counts watch_count > 0
    EVER, no recency constraint) used to pass this straight through -- a
    40-channel household producing one watch in 200 days is a blind sensor,
    not an idle household."""
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40, stats_since=now - 400 * 86400,
                       coverage=buckets(now, 24 * 45, ticks=240))
    chans = list(usage["channels"].values())
    for rec in chans[:39]:
        rec["last_watched"] = now - 200 * 86400
    chans[39]["last_watched"] = now - 6 * 86400
    result = gates.evaluate(usage, rows_total=1440, never_watched=1400, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("distinct channels watched in the last 7" in a for a in result["alerts"])


def test_never_watched_ceiling_trips(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40)
    result = gates.evaluate(usage, rows_total=1000, never_watched=900, now=now,
                            thresholds=THRESHOLDS)     # 90% > 60% ceiling
    assert result["ok"] is False
    assert any("never-watched fraction" in a for a in result["alerts"])


def test_too_few_distinct_channels_trips(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=3)        # < 5 distinct
    result = gates.evaluate(usage, rows_total=1440, never_watched=100, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("distinct channels" in a for a in result["alerts"])


def test_young_dataset_trips_the_age_gate(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40, stats_since=now - 5 * 86400)
    result = gates.evaluate(usage, rows_total=1440, never_watched=200, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("only 5 days" in a for a in result["alerts"])


def test_low_coverage_trips(gates):
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40,
                       coverage=buckets(now, 10, ticks=240))   # sparse
    result = gates.evaluate(usage, rows_total=1440, never_watched=200, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("is below" in a for a in result["alerts"])


def test_empty_usage_never_reads_as_nobody_watched(gates):
    now = 1_700_000_000.0
    result = gates.evaluate({}, rows_total=1440, never_watched=1440, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False


def test_zero_rows_total_trips_the_ceiling_gate(gates):
    """An ORM query returning nothing (schema change, bad filter, empty profile)
    must not read as `ok=True` on a lineup of zero channels -- a zero-row
    lineup is never a believable dataset."""
    now = 1_700_000_000.0
    usage = usage_with(now, watched_channels=40)
    result = gates.evaluate(usage, rows_total=0, never_watched=0, now=now,
                            thresholds=THRESHOLDS)
    assert result["ok"] is False
    assert any("rows_total is 0" in a for a in result["alerts"])


def test_unobservable_alert_above_threshold(gates):
    msg = gates.unobservable_alert(95, 100)
    assert msg is not None
    assert "unobservable" in msg


def test_unobservable_alert_below_threshold(gates):
    assert gates.unobservable_alert(50, 100) is None


def test_unobservable_alert_at_threshold_passes(gates):
    # MAX_UNOBSERVABLE_FRACTION is 0.90; the gate must pass AT the threshold.
    assert gates.unobservable_alert(90, 100) is None


def test_unobservable_alert_zero_rows_total(gates):
    assert gates.unobservable_alert(10, 0) is None
