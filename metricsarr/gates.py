"""Metricsarr gates -- coverage density + plausibility. Pure, stdlib only.

These gates are the difference between "the user stopped watching" and "the sensor
went blind". The catastrophic failure this module exists to catch is not a missing
usage.json (storage.py already handles that) -- it is a usage.json that is present,
parseable, old enough, coverage-complete, and full of ZEROS. That is exactly what a
Dispatcharr upgrade that reshapes the Redis keyspace, a Redis flush, or a wedged
collector produces. Every naive gate passes and the plugin concludes the household
watches nothing. Spec S8.

coverage_fraction measures sampling DENSITY, never DATA VALIDITY. It is computed
from sessionizer._mark_coverage(now), which is actually the first line of
sessionizer.observe() -- and run_tick only reaches observe() AFTER a successful
_sample() (collector.py's Redis SCAN + pipelined SCARD). So a Redis SCAN error
DOES depress coverage (correctly, fail-closed): run_tick's `except` around
_sample() returns before observe() ever runs, so that tick marks no bucket at
all. What coverage_fraction cannot see is a collector that keeps SAMPLING
successfully while the SAMPLE ITSELF is meaningless -- e.g. a Dispatcharr
upgrade that reshapes the Redis keyspace so the SCAN pattern silently matches
nothing, or matches keys whose client counts no longer mean what they used to.
In that shape the tick loop keeps running, _sample() keeps "succeeding" (it
just returns an empty or wrong `present` dict), gaps stay ~15s, and coverage
reads 1.0 FOREVER -- coverage is structurally incapable of detecting a
blind-but-ticking collector. The watch-plausibility gates below (recent-distinct-
channel count, above all) are the ONLY defense against that failure mode, which is
exactly why they matter as much as density does. The residual blind window after a
collector goes silently blind is bounded by RECENT_WATCH_WINDOW_S *because of* the
recency gate -- weakening that gate (or removing the recency constraint from the
distinct-channel count) lengthens the blind window without limit. Do not try to fix
this with a "recent coverage" gate; coverage cannot see it no matter how you window
it, because the bug is upstream of what coverage measures.

coverage_fraction itself: an hour counts as covered only if it holds >=90% of the
ticks it should have (derived from the CONFIGURED poll_interval_s, not the
throttled one) and its largest gap fits inside client_gap_grace_s -- a whole watch
session could have opened and closed unseen inside a bigger gap. It iterates the
EXPECTED hour range, anchored to `now`, and looks each key up with .get(), never
coverage.keys() -- an hour with zero ticks produces no bucket at all (sessionizer
only creates a bucket when it ticks), so iterating keys() would let a collector
that was dead for 20 of 24 hours score 100% coverage, and (since coverage is
pruned well past the query window) would silently read the OLDEST surviving hours
instead of the most recent ones once the collector had been dead longer than the
window. That is precisely the failure this gate exists to catch; do not "optimize"
this into a keys() walk.

All gates pass exactly AT their threshold: coverage == MIN_COVERAGE passes, exactly
MIN_DISTINCT_CHANNELS recent-distinct channels passes, a watch exactly
RECENT_WATCH_WINDOW_S old passes, an unobservable fraction exactly
MAX_UNOBSERVABLE_FRACTION passes. This is coherent (the gate is "fail past the
line"), just worth knowing when reading a borderline alert.

The never-watched ceiling (I2) is denominated on the JUDGED population
(never_watched + too_new + tuned_never_qualified + watched), passed in
explicitly as `judged_total` -- NOT on `rows_total`, the full ORM channel
count. A real lineup routinely excludes most of its rows by policy (auto-
created slots, whole groups, a name regex) -- e.g. 1010 of 1440 channels on a
real box -- so `never_watched / rows_total` has a hard ceiling far below any
sane threshold and could never fire under any failure. Rebasing on the
judged population instead means a perfectly healthy household can show 80-90%
never-watched among the channels it was ever asked to judge, so the default
ceiling is deliberately high (0.98): it exists to catch the mass-casualty
shape -- essentially EVERY judged channel looking dead -- not to flag a
normal lineup with a large disused tail.

A sustained throttle is also a legitimate way to trip these gates, not just a
sensor going blind: doubling the poll interval to 120s against a 15s-configured
expectation yields ~30 ticks/hour where ~216 are needed (0.9 * 3600/15), so
coverage_fraction reads 0.0 for every such hour and evaluate() returns a permanent
ok=False. That is the correct fail-safe behavior, but it will look like a false
alarm in the field -- a throttled-but-honest collector and a dead one produce the
same alert text. A caller diagnosing "why did this trip" should check the actual
tick rate before assuming the collector went blind.

Every gate failure returns ok=False with a loud, human-readable alert. ok=False
means report only, never act.
"""
from __future__ import annotations


def _load_sibling(name):
    """Load a sibling stdlib-only module by FILE PATH (mirrors what
    tests/conftest.py already does for standalone module loading), so this
    works whether gates.py is imported as part of the plugin package OR
    loaded standalone (as the test suite's load_pure() does, with no parent
    package for a relative import to resolve against). Used once, below, to
    delegate to sessionizer.bucket_key instead of duplicating it (M3)."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        f"_metricsarr_gates_sibling_{name}",
        pathlib.Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from . import sessionizer as _sessionizer
except ImportError:                     # standalone (non-package) import path
    _sessionizer = _load_sibling("sessionizer")

MIN_COVERAGE = 0.90
RECENT_WATCH_WINDOW_S = 7 * 86400
MIN_DISTINCT_CHANNELS = 5
MAX_UNOBSERVABLE_FRACTION = 0.90

# M3: NOT a hand-duplicate of sessionizer.bucket_key -- delegates to the same
# implementation so the two modules can never silently disagree on the UTC
# hour-bucket format.
_bucket_key = _sessionizer.bucket_key


def coverage_fraction(coverage, now, window_days, poll_interval_s,
                      client_gap_grace_s):
    """Fraction of the window's hours that were genuinely SAMPLED.

    An hour counts as covered only if it holds >=90% of the ticks it should have
    AND its largest gap fits inside the client grace -- otherwise a whole session
    could have opened and closed unseen inside that gap.
    """
    coverage = coverage or {}
    total_hours = int(window_days * 24)
    if total_hours <= 0:
        return 0.0

    expected_ticks = 3600.0 / max(float(poll_interval_s), 1.0)
    needed = 0.9 * expected_ticks

    covered = 0
    for i in range(total_hours):
        bucket = coverage.get(_bucket_key(now - i * 3600))
        if not bucket:
            continue
        if bucket.get("ticks", 0) < needed:
            continue
        if bucket.get("max_gap_s", 0.0) > float(client_gap_grace_s):
            continue
        covered += 1

    return covered / float(total_hours)


def evaluate(usage, rows_total, never_watched, now, thresholds, judged_total):
    """Decide whether the dataset may be believed. ok=False => report only.

    `judged_total` (I2) is the JUDGED population -- never_watched + too_new +
    tuned_never_qualified + watched -- the denominator for the never-watched
    ceiling. It is REQUIRED, not defaulted: `rows_total` includes every
    excluded/unobservable channel too, and denominating on it makes the
    ceiling structurally unreachable on a real lineup (see the module
    docstring). `reports.build_model` is the sole real caller and always has
    this count on hand.

    The returned dict also carries `immature` (F1): whether the dataset is
    still younger than `unused_threshold_days`, computed from the SAME
    un-rounded `age_days` used for the warmup alert above. Callers deciding
    whether a not-ok gate should page (notify_report.sensor_blind) must read
    THIS field rather than re-deriving maturity from a rounded age -- that
    rounding is exactly the seam that let a healthy, still-warming-up dataset
    fire a false critical.
    """
    alerts = []
    usage = usage or {}
    channels = usage.get("channels") or {}
    meta = usage.get("meta") or {}

    if not channels:
        alerts.append("usage data is empty - the collector has recorded nothing")

    stats_since = meta.get("stats_since")
    window_days = float(thresholds.get("unused_threshold_days", 30))
    if not stats_since:
        alerts.append("usage data has no stats_since - dataset is not trustworthy")
        age_days = 0.0
    else:
        age_days = (now - float(stats_since)) / 86400.0
        if age_days < window_days:
            alerts.append(
                f"only {int(age_days)} days of data (need {int(window_days)})")

    # F1: computed from the SAME un-rounded age_days used for the alert above,
    # not from a rounded `tracked_days` a caller might store -- notify_report's
    # sensor_blind() used to compare against round(age_days, 1), which rounds
    # 29.96 up to 30.0 and pages on a still-immature dataset. Callers must
    # read maturity from THIS field, never re-derive it from a rounded age.
    immature = (not stats_since) or age_days < window_days

    coverage = coverage_fraction(
        meta.get("coverage"), now,
        window_days=min(window_days, max(age_days, 1.0)),
        poll_interval_s=float(thresholds.get("poll_interval_s", 15.0)),
        client_gap_grace_s=float(thresholds.get("client_gap_grace_s", 90.0)))
    if coverage < MIN_COVERAGE:
        alerts.append(f"coverage {coverage:.0%} is below {MIN_COVERAGE:.0%} - "
                      "the collector was blind for part of the window")

    recent = [rec for rec in channels.values()
              if (rec.get("last_watched") or 0) >= now - RECENT_WATCH_WINDOW_S]
    if not recent:
        alerts.append("no qualified watches in the last 7 days - the sensor is "
                      "blind, not the household idle")

    distinct = sum(1 for rec in channels.values() if rec.get("watch_count", 0) > 0)
    if distinct < MIN_DISTINCT_CHANNELS:
        alerts.append(f"only {distinct} distinct channels ever watched "
                      f"(need {MIN_DISTINCT_CHANNELS}) - implausible")

    recent_distinct = sum(1 for rec in channels.values()
                          if (rec.get("last_watched") or 0) >= now - RECENT_WATCH_WINDOW_S
                          and rec.get("watch_count", 0) > 0)
    if recent_distinct < MIN_DISTINCT_CHANNELS:
        alerts.append(f"only {recent_distinct} distinct channels watched in the last 7 "
                      f"days (need {MIN_DISTINCT_CHANNELS}) - the sensor is blind, not "
                      "the household idle")

    ceiling = float(thresholds.get("never_watched_ceiling", 0.98))
    if not rows_total:
        alerts.append("rows_total is 0 - the channel lineup is empty")
    elif judged_total:
        # I2: denominated on the JUDGED population, not rows_total -- see the
        # module docstring. judged_total==0 with rows_total>0 means every
        # channel was excluded/unobservable, which the unobservable-fraction
        # alert (wired in by reports.build_model) already covers; skip rather
        # than divide by zero.
        fraction = never_watched / float(judged_total)
        if fraction > ceiling:
            alerts.append(f"never-watched fraction {fraction:.0%} of judged "
                          f"channels exceeds the {ceiling:.0%} ceiling - this "
                          f"is a bug report, not a policy")

    return {"ok": not alerts, "alerts": alerts, "coverage": coverage,
            "immature": immature}


def unobservable_alert(unobservable, judged_total):
    """Task 8 calls this; evaluate() cannot -- it isn't given the count.

    Additive, not wired into evaluate(): a channel is "unobservable" for reasons
    evaluate() has no visibility into (e.g. no metadata key ever seen for it), so
    the caller that computes that count is responsible for surfacing this alert
    alongside evaluate()'s own alerts.

    `judged_total` (like the never-watched ceiling I2) is the JUDGED population
    (never_watched + too_new + tuned_never_qualified + watched), not rows_total
    (the full ORM channel count). Unobservable channels are excluded from the
    judged set, so the fraction is denominated on the channels actually eligible
    for judgment, not every channel in the lineup.
    """
    if judged_total and unobservable / float(judged_total) > MAX_UNOBSERVABLE_FRACTION:
        return (f"{unobservable / judged_total:.0%} of the judged channels are unobservable - "
                "the dataset cannot support any conclusion")
    return None
