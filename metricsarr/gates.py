"""Metricsarr gates -- coverage density + plausibility. Pure, stdlib only.

These gates are the difference between "the user stopped watching" and "the sensor
went blind". The catastrophic failure this module exists to catch is not a missing
usage.json (storage.py already handles that) -- it is a usage.json that is present,
parseable, old enough, coverage-complete, and full of ZEROS. That is exactly what a
Dispatcharr upgrade that reshapes the Redis keyspace, a Redis flush, or a wedged
collector produces. Every naive gate passes and the plugin concludes the household
watches nothing. Spec S8.

coverage_fraction measures sampling DENSITY, not liveness: an hour counts as covered
only if it holds >=90% of the ticks it should have (derived from the CONFIGURED
poll_interval_s, not the throttled one) and its largest gap fits inside
client_gap_grace_s -- a whole watch session could have opened and closed unseen
inside a bigger gap. It iterates the EXPECTED hour range and looks each key up with
.get(), never coverage.keys() -- an hour with zero ticks produces no bucket at all
(sessionizer only creates a bucket when it ticks), so iterating keys() would let a
collector that was dead for 20 of 24 hours score 100% coverage. That is precisely
the failure this gate exists to catch; do not "optimize" this into a keys() walk.

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

    ceiling = float(thresholds.get("never_watched_ceiling", 0.60))
    if rows_total:
        fraction = never_watched / float(rows_total)
        if fraction > ceiling:
            alerts.append(f"never-watched fraction {fraction:.0%} exceeds the "
                          f"{ceiling:.0%} ceiling - this is a bug report, not a policy")

    return {"ok": not alerts, "alerts": alerts, "coverage": coverage}
