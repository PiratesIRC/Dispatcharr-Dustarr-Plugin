"""Metricsarr gates -- coverage density + plausibility. Pure, stdlib only.

These gates are the difference between "the user stopped watching" and "the sensor
went blind". The catastrophic failure this module exists to catch is not a missing
usage.json (storage.py already handles that) -- it is a usage.json that is present,
parseable, old enough, coverage-complete, and full of ZEROS. That is exactly what a
Dispatcharr upgrade that reshapes the Redis keyspace, a Redis flush, or a wedged
collector produces. Every naive gate passes and the plugin concludes the household
watches nothing. Spec S8.

coverage_fraction measures sampling DENSITY, never DATA VALIDITY. It is computed
from sessionizer._mark_coverage(now), which is called unconditionally at the top
of every tick, BEFORE any channel scan. So after a Redis flush, a keyspace change,
or a silently-wedged collector, the tick loop keeps running, gaps stay ~15s, and
coverage reads 1.0 FOREVER -- coverage is structurally incapable of detecting a
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

import time

MIN_COVERAGE = 0.90
RECENT_WATCH_WINDOW_S = 7 * 86400
MIN_DISTINCT_CHANNELS = 5
MAX_UNOBSERVABLE_FRACTION = 0.90


def _bucket_key(ts):
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


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


def evaluate(usage, rows_total, never_watched, now, thresholds):
    """Decide whether the dataset may be believed. ok=False => report only."""
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

    ceiling = float(thresholds.get("never_watched_ceiling", 0.60))
    if not rows_total:
        alerts.append("rows_total is 0 - the channel lineup is empty")
    else:
        fraction = never_watched / float(rows_total)
        if fraction > ceiling:
            alerts.append(f"never-watched fraction {fraction:.0%} exceeds the "
                          f"{ceiling:.0%} ceiling - this is a bug report, not a policy")

    return {"ok": not alerts, "alerts": alerts, "coverage": coverage}


def unobservable_alert(unobservable, rows_total):
    """Task 8 calls this; evaluate() cannot -- it isn't given the count.

    Additive, not wired into evaluate(): a channel is "unobservable" for reasons
    evaluate() has no visibility into (e.g. no metadata key ever seen for it), so
    the caller that computes that count is responsible for surfacing this alert
    alongside evaluate()'s own alerts.
    """
    if rows_total and unobservable / float(rows_total) > MAX_UNOBSERVABLE_FRACTION:
        return (f"{unobservable / rows_total:.0%} of the lineup is unobservable - "
                "the dataset cannot support any conclusion")
    return None
