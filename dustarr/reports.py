"""Dustarr reports -- join the ORM channel universe against usage.json.

THE INVARIANT (spec S6): the channel universe is the ORM. usage.json is a SPARSE
OVERLAY of watched channels only. A channel absent from it is NEVER-WATCHED --
the default, expected state, not an error. That is the plugin's entire purpose.

`usage.json` is UNTRUSTED FILE INPUT: storage.load() validates only that the top
level is a dict, never per-record field types. `_sanitize_usage()` coerces every
per-channel field AND every `meta` field (`stats_since`, `coverage[*]`) once, at
the top of build_model, so neither this module's own reads nor gates.evaluate()
(which is not None/str-tolerant) can be crashed by a malformed record or a
malformed meta block.

`too_new` is a PEER of `never_watched` in `counts` and in the gate/notify never-
watched number, NOT a sub-count of it -- a fresh M3U import must not inflate the
one actionable headline number or spuriously trip the >60% ceiling gate. Its
entries still live inside the `never_watched` LIST (with reason="too_new") so
Task 9 can render them there. `counts` sums to `total_channels`:
    never_watched + too_new + tuned_never_qualified + watched + excluded
    + unobservable == total_channels
"""
from __future__ import annotations

import base64
import csv
import html as html_mod
import io
import math
import os
import time

try:
    from . import gates, gateway, sessionizer
except ImportError:                     # standalone (non-package) import path
    import gates
    import gateway
    import sessionizer

# M3: same shape as sessionizer._blank_record(now) (the record a live tick
# creates), not hand-duplicated -- calling it with now=None gives exactly the
# all-zero/all-None shape this module needs as the default for a channel
# absent from usage.channels (THE INVARIANT above).
EMPTY = sessionizer._blank_record(None)

# Must match the floor on the recent_window_days setting in plugin.py
# (_NUMERIC_FLOORS["recent_window_days"]). sessionizer.py writes last_watched
# only when a session finalizes and last_tuned only when a session opens, so a
# channel that has been streaming continuously since before this many days ago
# has both timestamps stale and would otherwise be reported as abandoned while
# it is on screen. See the comment above _NUMERIC_FLOORS in plugin.py for the
# full argument.
MIN_COLD_WINDOW_DAYS = 7.0

# Sort sentinel for a channel that was never watched. Large rather than small so
# that sorting the "days since" column ascending puts the most recently watched
# first and the never-watched at the far end, which is where a reader looking
# for retirement candidates expects them. It is a float so the column can never
# fall into the sort script's text-comparison path.
NEVER_SORT = 99999.0


def _coerce_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_timestamp(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_coverage_bucket(bucket):
    bucket = bucket or {}
    return {
        "ticks": _coerce_int(bucket.get("ticks")),
        "max_gap_s": _coerce_float(bucket.get("max_gap_s")),
    }


def _sanitize_meta(meta):
    """Coerce untrusted usage.json `meta` fields (R1, the other half of I4).

    `meta.stats_since` feeds `float(stats_since)` both here and in
    gates.evaluate(); `meta.coverage[*]` feeds gates.coverage_fraction()'s
    numeric comparisons (`bucket.get("ticks") < needed`,
    `bucket.get("max_gap_s") > client_gap_grace_s`). Neither tolerates
    None/string garbage -- coerce at the same trust boundary as the
    per-channel records, so a malformed meta block degrades instead of
    raising.
    """
    meta = meta or {}
    raw_coverage = meta.get("coverage") or {}
    coverage = {key: _sanitize_coverage_bucket(bucket)
                for key, bucket in raw_coverage.items()}
    return {
        "stats_since": _coerce_timestamp(meta.get("stats_since")),
        "coverage": coverage,
    }


def _sanitize_usage(usage):
    """Coerce untrusted usage.json record + meta fields (I4, R1).

    storage.load() only validates that the top level is a dict; per-record
    values (watch_count, watch_seconds, timestamps, ...) and `meta` fields
    are never coerced and can arrive as None, strings, or otherwise
    malformed. Every downstream reader in this module -- and gates.evaluate(),
    which is not None/str-tolerant -- assumes numeric fields are numeric.
    Coerce once, here, so a corrupt record or meta block degrades to
    zeros/None instead of raising.
    """
    usage = usage or {}
    raw_channels = usage.get("channels") or {}
    channels = {}
    for uuid, record in raw_channels.items():
        record = record or {}
        channels[uuid] = {
            "watch_count": _coerce_int(record.get("watch_count")),
            "watch_seconds": _coerce_float(record.get("watch_seconds")),
            "tune_count": _coerce_int(record.get("tune_count")),
            "last_watched": _coerce_timestamp(record.get("last_watched")),
            "last_tuned": _coerce_timestamp(record.get("last_tuned")),
            "first_seen": _coerce_timestamp(record.get("first_seen")),
        }
    return {"channels": channels, "meta": _sanitize_meta(usage.get("meta"))}


def _setting_number(settings, key, default, cast):
    """Coerce a user setting (M3).

    `settings.get(key, default) or default` silently turns an intentional 0
    (e.g. top_n=0, "show nothing") into `default` because 0 is falsy. Only a
    genuinely MISSING or empty-string value should fall back to `default`.
    """
    value = settings.get(key, default)
    if value is None or value == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _entry(row, record, reason, now):
    created = row.created_at
    age_days = ((now - created) / 86400.0) if created else None
    last_watched = record.get("last_watched")
    watch_count = int(record.get("watch_count") or 0)
    watch_seconds = float(record.get("watch_seconds") or 0.0)

    # Clamped at zero: a forward clock step (or a corrected backward one) can
    # put last_watched in the future, and a negative age would sort a stale
    # channel ahead of one watched a minute ago. Same try/except shape as
    # _fmt_local/_iso_utc below: last_watched is untrusted file input (see the
    # module docstring), and an unparseable value must degrade to the same
    # result as never watched rather than raise out of render_html, which has
    # no exception net around it.
    days_since = None
    if last_watched:
        try:
            days_since = max(0.0, (now - float(last_watched)) / 86400.0)
        except (TypeError, ValueError, OSError):
            days_since = None
    # Denominated on qualified watches, the same population as `hours`, so the
    # two columns multiply back to each other. A watch_count <= 0 or a
    # non-finite result (an infinite or NaN watch_seconds, both untrusted
    # file input) degrades to the same "n/a"/-1.0 sentinel as an absent
    # value, rather than reaching the renderer as a negative or infinite
    # number.
    avg_minutes = (watch_seconds / watch_count / 60.0) if watch_count > 0 else None
    if avg_minutes is not None and not math.isfinite(avg_minutes):
        avg_minutes = None

    return {
        "uuid": row.uuid,
        "name": row.name,
        "group": row.group or "(no group)",
        "watch_count": watch_count,
        "hours": round(watch_seconds / 3600.0, 2),
        "last_watched": last_watched,
        # M1: "last watched 8 months ago" is the single highest-value signal
        # in the dataset for "what do I turn off" -- it was collected and
        # stored but never rendered. Formatted at entry-build time (via the
        # same _fmt_local the data-confidence header already uses) so the
        # HTML table renderer stays a generic column-driven loop.
        "last_watched_display": _fmt_local(last_watched),
        # Display value and sort key are separate on purpose: the sort script
        # falls back to text comparison unless BOTH compared cells parse as
        # numbers, so one "never" cell would order the whole column 9 above 10.
        "days_since_watched": (round(days_since, 1) if days_since is not None
                               else "never"),
        "days_since_watched_sort": (round(days_since, 1) if days_since is not None
                                    else NEVER_SORT),
        "avg_session_minutes": (round(avg_minutes, 1) if avg_minutes is not None
                                else "n/a"),
        "avg_session_minutes_sort": (round(avg_minutes, 1) if avg_minutes is not None
                                     else -1.0),
        "last_tuned": record.get("last_tuned"),
        "tune_count": int(record.get("tune_count") or 0),
        "age_days": round(age_days, 1) if age_days is not None else None,
        "reason": reason,
    }


def build_model(rows, usage, settings, now):
    """Join the ORM channel universe (`rows`) against the sparse usage overlay.

    Returns the model dict consumed by `summary_for_notify` and by Task 9's
    renderers -- see the module docstring for the `counts` sum invariant and
    the `too_new`-is-a-peer design. `group_rollup[*]["total"]` is the group's
    TRUE ORM channel count (every row in that group, including excluded/
    unobservable ones), not just the rows that landed in never/used/tuned_only
    (M2) -- so it can exceed `["never"] + <watched in that group>`. (R4) A
    group only gets a `rollup` bucket at all if at least one of its rows
    landed in never/used/tuned_only -- a group whose entire membership is
    excluded/unobservable is ABSENT from `group_rollup`. Consequently
    `sum(g["total"] for g in group_rollup)` is NOT `total_channels` and must
    never be used as a denominator; use `model["total_channels"]` directly.

    A channel whose stream profile is non-proxying ("unobservable") but that
    nonetheless carries real watch history (profile was changed to non-
    proxying AFTER it was watched) is surfaced in the usage rankings rather
    than hidden in the `unobservable` bucket (M1).
    """
    usage = _sanitize_usage(usage)
    channels = usage["channels"]
    meta = usage["meta"]
    exclusions = gateway.parse_exclusions(settings)

    # R3: a negative top_n (e.g. -1) must not silently become `used[:-1]`
    # (Python slice semantics would drop the LAST entry) -- clamp to >= 0.
    # An explicit top_n=0 ("show nothing") passes through unchanged (M3).
    top_n = max(0, _setting_number(settings, "top_n", 20, int))
    threshold_days = _setting_number(settings, "unused_threshold_days", 30, float)

    never, tuned_only, used, excluded, unobservable = [], [], [], [], []

    for row in rows:
        reason = gateway.classify(row, exclusions, now)
        # THE INVARIANT: a channel absent from usage.channels is never-watched,
        # not an error -- .get() with an EMPTY default, never a KeyError.
        record = channels.get(row.uuid) or EMPTY

        if reason == "unobservable" and record.get("watch_count", 0) > 0:
            # M1: real usage survives a later profile change to non-proxying.
            entry = _entry(row, record, "watched", now)
            # The collector cannot see this channel at all any more, so a
            # silence since the profile changed is not evidence of disuse. The
            # cold classification below skips these; the rankings still show
            # them, which is the M1 behaviour.
            entry["unobservable_profile"] = True
            used.append(entry)
            continue
        if reason == "unobservable":
            unobservable.append(_entry(row, record, reason, now))
            continue
        if reason:
            excluded.append(_entry(row, record, reason, now))
            continue

        if record.get("watch_count", 0) > 0:
            used.append(_entry(row, record, "watched", now))
        elif record.get("tune_count", 0) > 0:
            # Tried and abandoned inside min_watch_seconds: BROKEN, not unused.
            tuned_only.append(_entry(row, record, "tuned_never_qualified", now))
        else:
            age = ((now - row.created_at) / 86400.0) if row.created_at else None
            too_new = age is not None and age < threshold_days
            never.append(_entry(row, record, "too_new" if too_new
                                else "never_watched", now))

    used.sort(key=lambda c: (c["watch_count"], c["hours"]), reverse=True)

    group_totals = {}
    for row in rows:
        group = row.group or "(no group)"
        group_totals[group] = group_totals.get(group, 0) + 1

    rollup = {}
    for entry in never + used + tuned_only:
        bucket = rollup.setdefault(entry["group"], {"group": entry["group"],
                                                    "never": 0, "judged": 0,
                                                    "total": 0})
        # `judged` is the mini bar's denominator: `total` is the group's TRUE
        # ORM count INCLUDING excluded rows, so a bar over it would assert a
        # proportion the data does not support.
        bucket["judged"] += 1
        if entry["reason"] in ("never_watched", "too_new"):
            bucket["never"] += 1
    for bucket in rollup.values():
        bucket["total"] = group_totals.get(bucket["group"], 0)
    group_rollup = sorted(rollup.values(), key=lambda g: g["never"], reverse=True)

    never_watched_entries = [e for e in never if e["reason"] == "never_watched"]
    too_new_entries = [e for e in never if e["reason"] == "too_new"]

    # I2: the never-watched ceiling is denominated on the JUDGED population,
    # not the full ORM row count -- `never` already holds BOTH never_watched
    # and too_new entries (module docstring), so this sum is exactly
    # never_watched + too_new + tuned_never_qualified + watched.
    judged_total = len(never) + len(used) + len(tuned_only)
    gate = gates.evaluate(usage, rows_total=len(rows),
                          never_watched=len(never_watched_entries), now=now,
                          thresholds=settings, judged_total=judged_total)
    # C1: a non-proxying stream profile never writes Redis keys, so those
    # channels are structurally invisible to the collector -- if most of the
    # judged population is unobservable, the dataset cannot support any conclusion.
    # evaluate() has no visibility into this count; wire it in here, using the
    # same JUDGED denominator (never_watched + too_new + tuned_only + watched).
    unobservable_msg = gates.unobservable_alert(len(unobservable), judged_total)
    if unobservable_msg:
        gate["alerts"].append(unobservable_msg)
        gate["ok"] = False

    stats_since = meta.get("stats_since")
    tracked_days = ((now - float(stats_since)) / 86400.0) if stats_since else 0.0

    # Recency, from data that has always been recorded. The window is clamped
    # to the dataset age so an empty section on a young dataset can say why:
    # "empty because nothing is cold" and "empty because the data does not
    # reach back that far" are different statements. It is never clamped
    # below MIN_COLD_WINDOW_DAYS: a shorter window can name a channel that is
    # on screen right now (see that constant's comment), so when the dataset
    # itself is younger than the floor, the plugin cannot answer the question
    # at all rather than answering it with a too-short window.
    requested_window = max(MIN_COLD_WINDOW_DAYS,
                           _setting_number(settings, "recent_window_days",
                                           30, float))
    # A negative or otherwise nonsensical dataset age (e.g. a stats_since in
    # the future) must take the too-young path rather than be treated as
    # smaller than the requested window and collapse it to the floor.
    cold_window_too_young = (not tracked_days
                             or tracked_days < MIN_COLD_WINDOW_DAYS)
    if cold_window_too_young:
        cold_window = requested_window
        cold_window_clamped = False
    elif tracked_days < requested_window:
        cold_window = round(tracked_days, 1)
        cold_window_clamped = True
    else:
        cold_window = requested_window
        cold_window_clamped = False

    cold_abandoned, cold_still_tried = [], []
    if not cold_window_too_young:
        for entry in used:
            # Never judge a channel the collector cannot observe (see the M1
            # branch above): its silence carries no information.
            if entry.get("unobservable_profile"):
                continue
            last_watched = entry.get("last_watched")
            if not last_watched:
                continue
            # This recomputes days-since from the raw stored `last_watched`
            # rather than reusing entry["days_since_watched_sort"] (built
            # above in _entry from the same raw value, but coerced/rounded
            # there first). The two agree today only because _sanitize_usage
            # coerces last_watched to a float or None before either
            # computation ever sees it. If that coercion or either
            # computation changes, this comparison and the rendered sort key
            # can silently disagree about which channels are cold. Keep them
            # consistent.
            if (now - float(last_watched)) / 86400.0 < cold_window:
                continue
            last_tuned = entry.get("last_tuned")
            still_tried = (bool(last_tuned)
                          and (now - float(last_tuned)) / 86400.0 < cold_window)
            # Tried recently and abandoned inside the watch threshold means
            # BROKEN, not unwanted. Conflating the two is how a metrics tool
            # recommends disabling the channels somebody is fighting hardest
            # to watch.
            (cold_still_tried if still_tried else cold_abandoned).append(entry)

    cold_abandoned.sort(key=lambda e: e["days_since_watched_sort"], reverse=True)
    cold_still_tried.sort(key=lambda e: e["days_since_watched_sort"], reverse=True)

    # I1: disjoint from most_used -- take up to top_n from the tail, but never
    # more than what's left over once the head (most_used) claims its share.
    least_n = max(0, min(top_n, len(used) - top_n))

    return {
        "generated_at": now,
        "generated_at_local": time.strftime("%Y-%m-%d %H:%M %Z",
                                            time.localtime(now)),
        "stats_since": stats_since,
        "tracked_days": round(tracked_days, 1),
        "coverage": gate["coverage"],
        "total_channels": len(rows),
        "never_watched": never,
        "tuned_never_qualified": tuned_only,
        "most_used": used[:top_n],
        "least_used": list(reversed(used))[:least_n],
        "excluded": excluded,
        "unobservable": unobservable,
        "group_rollup": group_rollup,
        "cold_abandoned": cold_abandoned,
        "cold_still_tried": cold_still_tried,
        "cold_window_days": cold_window,
        "cold_window_clamped": cold_window_clamped,
        "cold_window_too_young": cold_window_too_young,
        "gate": gate,
        "counts": {
            "never_watched": len(never_watched_entries),
            "too_new": len(too_new_entries),
            "tuned_never_qualified": len(tuned_only),
            "watched": len(used),
            "excluded": len(excluded),
            "unobservable": len(unobservable),
        },
    }


def summary_for_notify(model, reports_built=None):
    """The allowlisted notification summary.

    It used to carry a `report_url` pointing at the nginx-served copy of the
    report. That copy no longer exists (see the renderer note below), so the
    key is gone rather than left holding a dead link.

    `reports_built` is the running total of reports this plugin has PUBLISHED,
    for a consumer that wants to display it. It is OMITTED rather than sent as
    0 when the caller does not supply one: a missing key reads as "this sender
    does not report the number", while a 0 reads as "it has never built one",
    and a badge cannot tell those apart if the absent case is spelled 0.
    """
    if reports_built is not None:
        return dict(_summary_body(model), reports_built=int(reports_built))
    return _summary_body(model)


def _summary_body(model):
    return {
        "tracked_days": model["tracked_days"],
        "coverage": model["coverage"],
        "total_channels": model["total_channels"],
        "never_watched": model["counts"]["never_watched"],
        "tuned_never_qualified": model["counts"]["tuned_never_qualified"],
        "top": [{"name": c["name"], "watch_count": c["watch_count"],
                 "hours": c["hours"]} for c in model["most_used"][:5]],
        "alerts": model["gate"]["alerts"],
    }


# --------------------------------------------------------------------------
# Renderers (Task 9).
#
# NOTHING HERE IS SERVED OVER HTTP, DELIBERATELY. The report used to be
# written into /data/logos/, which Dispatcharr's nginx serves to the whole
# LAN with no authentication at all -- convenient, and an unauthenticated
# listing of every channel this household watches. It is now written next to
# the CSV in /config/dustarr/, which sits under Dispatcharr's existing bind
# mount and so is a real folder on the host. Do not move it back under
# /data/logos/, and do not add a URL field that would invite someone to.
#
# The page must still be fully self-contained (inline CSS/JS, no external
# assets): it is opened straight off disk as a file:// URL and is also mailed
# as an attachment, so it must render on a TV browser with no network access
# to a CDN and no server to resolve relative paths against.
#
# Credential safety: only allowlisted per-channel fields are ever rendered
# (name, uuid, group, counts, timestamps) -- never Stream.url or anything
# that could carry provider credentials, which live in stream URLs in this
# deployment.
# --------------------------------------------------------------------------

REPORT_HTML = "report.html"

# The project's own issue tracker, shown in the report footer. Duplicated from
# plugin.py's ISSUES_URL rather than imported: this module must not import
# plugin.py (that is the direction the loader depends on), and a test binds the
# two strings together so they cannot drift apart silently.
ISSUES_URL = "https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/issues"
REPO_URL = "https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin"

LOGO_FILE = "logo.png"
_logo_cache = []        # one-slot cache: [] not consulted yet, [value] resolved


def logo_data_uri():
    """-> a `data:image/png;base64,...` string, or "" if the file is unusable.

    EMBEDDED, never linked. This page is opened off disk as a file:// URL and
    is also mailed as an attachment, so a relative path resolves against
    nothing and a remote URL is blocked by default in most mail clients (and
    this project's repository is private, so a raw GitHub link would 404 for
    everyone anyway). A data URI is the only form that survives both.

    Returns "" rather than raising on any failure. `render_html` has no safety
    net -- `write_report` catches OSError only -- so a missing or unreadable
    logo must cost the header image and nothing else.

    Cached because `render_html` is called per build and the file is 45 KB.
    The cache holds the FAILURE too: a missing file should not be re-read on
    every render.
    """
    if not _logo_cache:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                LOGO_FILE)
            with open(path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
            _logo_cache.append(f"data:image/png;base64,{encoded}")
        except Exception:
            _logo_cache.append("")
    return _logo_cache[0]


GAP_PX = 2          # surface gap between adjacent segments
MIN_SEG_PX = 2      # a real category must never vanish sub-pixel
BAR_H = 22          # mark spec: bars <= 24px thick

# (legend label, counts key, css class). Order is for READING -- the palette
# was validated all-pairs, so no order is unsafe. See spec section 6.
_SEGMENT_ORDER = (
    ("Never watched", "never_watched", "seg-never"),
    ("Watched", "watched", "seg-watched"),
    ("Too new", "too_new", "seg-toonew"),
    ("Tuned, never qualified", "tuned_never_qualified", "seg-tuned"),
)


def _coerce_segment_count(count):
    """TOTAL: coerce a raw segment count to a non-negative int, never raise.

    `count or 0` does not save `int()` from a hostile value: `float('nan')`
    is TRUTHY (nan != 0), so `count or 0` passes it straight through and
    `int(nan)` raises `ValueError`; a non-numeric string raises too. NaN is
    rejected explicitly via self-inequality (true only for NaN, and it works
    across int/float/str alike, unlike `math.isnan` which only accepts
    floats) before `int()` ever sees it.

    `float('inf')`/`float('-inf')` are NOT caught by the NaN guard (infinity
    equals itself) and `int(inf)` raises `OverflowError`, a THIRD exception
    type beyond `TypeError`/`ValueError` -- caught explicitly alongside them
    so a non-finite float can never escape this function. Anything
    unparseable, negative, non-finite, or NaN degrades to 0 -- i.e. the
    segment is dropped by the caller's `> 0` filter, never an exception.
    """
    if count != count:  # NaN is the only value that is not equal to itself
        return 0
    try:
        value = int(count)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value > 0 else 0


def _fit_to_track(raw_widths, track, floor):
    """Floor every width to at least `floor`, then make the total exactly
    `track` -- TOTAL over its inputs, never over/under-shoots.

    Flooring tiny-but-real segments creates "debt" (sum(widths) > track).
    The debt is shed from EVERY segment currently above the floor, not just
    the single largest one: taking it all from one absorber only works while
    that absorber has enough room, and a stress shape (one huge segment plus
    many tiny ones, all now pinned at `floor`) has nowhere near enough --
    the tiny segments' combined floor debt outstrips what the one absorber
    can give up, and the old single-absorber clamp let the total overflow
    `track` (rects then run past the viewBox). Shedding proportionally across
    all above-floor segments, and re-running whenever a segment gets pinned
    to the floor mid-shed, always converges because the branch below only
    reaches this loop when `track >= n * floor` -- i.e. there is always
    enough headroom above the floor, collectively, to absorb the debt.

    Degenerate case: if the track cannot even fit `n * floor` (width so
    narrow, or so many segments, that even the floor doesn't fit), the
    defined behaviour is to abandon the floor and split the track evenly --
    every segment shrinks together rather than any width going negative or
    the total exceeding `track`.
    """
    n = len(raw_widths)
    if n == 0:
        return []
    if track < n * floor:
        even = track / n
        return [even] * n

    widths = [max(value, floor) for value in raw_widths]
    debt = sum(widths) - track
    while debt > 1e-9:
        donors = [i for i, w in enumerate(widths) if w > floor + 1e-9]
        if not donors:
            break  # unreachable given the track >= n*floor guard above
        share = debt / len(donors)
        remaining = 0.0
        for i in donors:
            take = min(share, widths[i] - floor)
            widths[i] -= take
            remaining += share - take
        debt = remaining
    return widths


def _svg_split_bar(segments, width=900):
    """A 100%-stacked bar over the JUDGED population.

    `segments` is [(label, count, css_class)]. Returns (svg, legend_html).

    Denominated on the judged population, NOT the ORM universe: over all 1440
    channels this bar spent ~70% of its ink on `excluded` -- the category
    explicitly outside judgment -- while the broken-channels list rendered as
    an unlabeled sliver.

    Zero-count segments are dropped ENTIRELY (no rect, no gap, no legend
    entry). That is why the palette is validated all-pairs rather than
    adjacent-only: any two segments can become neighbours.

    No in-bar text label: `.chart { width: 100% }` over a fixed-height
    viewBox with `preserveAspectRatio="none"` scales X but not Y, so any
    `<text>` glyph inside the bar stretches on desktop and squashes on a
    narrow phone screen (the report is emailed as an attachment and opened on
    phones) -- there is no aspect ratio at which in-bar text renders
    undistorted. Every count already appears, undistorted, in the legend
    directly below the bar and in a table on the same page -- that relief
    rule is what makes dropping the in-bar label safe rather than a loss of
    information.

    TOTAL over its inputs -- every count is coerced defensively
    (`_coerce_segment_count`: NaN, negative, non-numeric or None all degrade
    to a dropped segment rather than raising) and widths always fit the
    track (`_fit_to_track`, including the degenerate too-narrow-for-the-floor
    case). `write_report` catches only OSError, so a raise here escapes to
    run()'s catch-all -- there is no net under this function.
    """
    live = [(label, c, css) for label, count, css in segments
            if (c := _coerce_segment_count(count)) > 0]
    total = sum(count for _, count, _ in live)
    if not live or total <= 0:
        return (f'<svg class="chart" role="img" aria-label="Nothing judged yet"'
                f' viewBox="0 0 {width} {BAR_H}" preserveAspectRatio="none">'
                f'<rect class="track" x="0" y="0" width="{width}"'
                f' height="{BAR_H}" rx="4"/>'
                f'<title>Nothing judged yet</title></svg>', "")

    track = max(0.0, width - GAP_PX * (len(live) - 1))
    raw = [track * count / total for _, count, _ in live]
    widths = _fit_to_track(raw, track, MIN_SEG_PX)

    parts, legend, x = [], [], 0.0
    for (label, count, css), seg_w in zip(live, widths, strict=True):
        parts.append(f'<rect class="seg {css}" x="{x:.2f}" y="0"'
                     f' width="{seg_w:.2f}" height="{BAR_H}" rx="4"/>')
        legend.append(f'<li><span class="swatch {_esc(css)}"></span>'
                      f'{_esc(label)} <b>{count}</b></li>')
        x += seg_w + GAP_PX

    aria = ", ".join(f"{label} {count}" for label, count, _ in live)
    svg = (f'<svg class="chart" role="img" aria-label="Judged population: {_esc(aria)}"'
           f' viewBox="0 0 {width} {BAR_H}" preserveAspectRatio="none">'
           f'{"".join(parts)}<title>{_esc(aria)}</title></svg>')
    return svg, f'<ul class="legend">{"".join(legend)}</ul>'


METER_H = 14
# Read from gates.MIN_COVERAGE (not hardcoded) so the tick position and the
# "gate at N%" title text cannot silently drift out of sync if the gate moves.
GATE_PCT = gates.MIN_COVERAGE


def _svg_meter(fraction, gate_ok, width=280):
    """Sampling DENSITY, not confidence. Returns (svg, chip_html).

    Coverage attests to sampling density, never to data validity -- so length
    encodes coverage in ONE neutral hue and the gate verdict rides on a
    separate chip. Encoding the verdict as the bar's colour would paint a full
    green bar for a blind-but-ticking collector, which is this plugin's
    documented worst input.

    TOTAL over its inputs (see _svg_split_bar's note on the missing net):
    NaN, both infinities, None, non-numeric strings, an int too huge for
    `float()` to represent (raises `OverflowError`, not `ValueError`), and
    unexpected types all degrade to a sane default (0.0) rather than raising.
    """
    try:
        value = float(fraction)
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    if value != value or value in (float("inf"), float("-inf")):   # NaN / inf
        value = 0.0
    value = min(1.0, max(0.0, value))

    fill_w = width * value
    tick_x = width * GATE_PCT
    svg = (f'<svg class="meter" role="img"'
           f' aria-label="Sampling density {value:.1%}"'
           f' viewBox="0 0 {width} {METER_H}" preserveAspectRatio="none">'
           f'<rect class="track" x="0" y="3" width="{width}" height="8" rx="4"/>'
           f'<rect class="fill" x="0" y="3" width="{fill_w:.2f}" height="8" rx="4"/>'
           f'<rect class="tick" x="{tick_x:.2f}" y="0" width="1.5"'
           f' height="{METER_H}"/>'
           f'<title>Sampling density {value:.1%} (gate at {GATE_PCT:.0%})</title>'
           f'</svg>')

    if gate_ok:
        chip = '<span class="chip chip-ok"><b>&#10003;</b> sampling OK</span>'
    else:
        chip = '<span class="chip chip-bad"><b>&#9888;</b> not trustworthy</span>'
    return svg, chip


MINI_W = 100
MINI_H = 10


def _svg_mini_bar(never, judged, label):
    """Never-watched share of a group's JUDGED rows. TOTAL over its inputs.

    Denominated on `judged`, never `total` (see build_model's rollup loop) --
    a bar drawn over the ORM total would assert a proportion the data does
    not support. Coercion delegates to `_coerce_segment_count` (NaN checked
    BEFORE `int()`, not after -- checking after would never run, since a
    NaN already raised out of the `try` by then): NaN, both infinities, None,
    non-numeric strings and unexpected types all degrade rather than raise,
    and a ratio above 1 (an impossible but not-worth-crashing-over input) is
    clamped.
    """
    never = _coerce_segment_count(never)
    judged = _coerce_segment_count(judged)
    ratio = 0.0 if judged <= 0 else min(1.0, max(0.0, never / float(judged)))
    name = _esc(label)
    return (f'<svg class="mini" role="img"'
            f' aria-label="{name}: {never} of {judged} never watched"'
            f' viewBox="0 0 {MINI_W} {MINI_H}" preserveAspectRatio="none">'
            f'<rect class="track" x="0" y="1" width="{MINI_W}" height="8" rx="4"/>'
            f'<rect class="fill" x="0" y="1" width="{MINI_W * ratio:.2f}"'
            f' height="8" rx="4"/>'
            f'<title>{name}: {never} of {judged} judged never watched</title>'
            f'</svg>')


# Token layer, reworked 2026-08-05 against the Refactoring UI guidance
# (github.com/LovroPodobnik/refactoring-ui-skill). Three rules from it drive
# the shape of this block:
#
# 1. SPACING IS A SCALE, NOT ARBITRARY VALUES. --s1..--s6 step by roughly a
#    third each, because a linear ramp makes small steps look identical and
#    large ones look wild. Every margin and padding below picks a step. There
#    were 14 one-off values here before; anything not on the scale is a bug.
# 2. GREY IS A RAMP, AND HIERARCHY RIDES ON IT RATHER THAN ON `opacity`.
#    Six rules used to fade text with opacity. That works, and measurement
#    says the old values cleared 4.5:1 (4.58 to 7.78), so this is NOT a
#    contrast fix. It is a predictability fix: an opacity value paints a
#    DIFFERENT colour on every surface it lands on, so the ratio silently
#    moves whenever a background changes, and the fade also applies to
#    anything nested inside. --ink / --ink-muted / --ink-dim are measured
#    (5.24:1 at the weakest, on the light surface) and stay put.
# 3. LIGHT AND DARK DIFFER ONLY IN TOKEN VALUES. The dark block used three
#    `!important` overrides to win specificity fights it should never have
#    been in.
#
# DO NOT change --never / --watched / --tuned / --toonew / --track / --ok /
# --bad, or the two surface colours --bg (#fbfbfd, #14161a). That palette was
# validated all-pairs for colourblind safety AGAINST those exact surfaces with
# a validator that does not live in this repository, so the numbers cannot be
# re-derived here. tests/test_report_charts.py pins every one of them.
_CSS = """
:root {
  color-scheme: light dark;
  --never: #2a78d6; --watched: #1baf7a; --tuned: #e34948; --toonew: #898781;
  --track: #e1e0d9; --ok: #0ca30c; --bad: #d03b3b;

  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px;

  --bg: #fbfbfd; --raised: #ffffff; --zebra: #f7f8fa; --head: #f2f3f6;
  --ink: #16181d; --ink-muted: #5c616b; --ink-dim: #656a76;
  --line: #e3e5ea; --line-soft: #e6e8ec;
  --warn-bg: #fff4e5; --warn-line: #ffb84d; --warn-ink: #7a4b00;
  --lift: 0 1px 2px rgba(16, 18, 29, .05), 0 4px 12px rgba(16, 18, 29, .04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --never: #3987e5; --watched: #199e70; --tuned: #e66767; --toonew: #898781;
    --track: #2c2c2a; --ok: #0ca30c; --bad: #d03b3b;

    --bg: #14161a; --raised: #1a1d22; --zebra: #191c21; --head: #1e2127;
    --ink: #e8eaed; --ink-muted: #a7adb8; --ink-dim: #9aa0ab;
    --line: #2a2e35; --line-soft: #262a31;
    --warn-bg: #2e2312; --warn-line: #8a6320; --warn-ink: #f2c98a;
    --lift: 0 1px 2px rgba(0, 0, 0, .35), 0 4px 12px rgba(0, 0, 0, .25);
  }
}
body { font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
       margin: 0; padding: var(--s5); background: var(--bg); color: var(--ink); }
/* The logo sits beside the title rather than above it, so the masthead costs
   one line of vertical space instead of three. `align-items: center` keeps the
   disc optically centred against the two-line title block. */
.masthead { display: flex; align-items: center; gap: var(--s3);
            margin-bottom: var(--s5); }
.mark { flex: none; width: 48px; height: 48px; display: block; }
/* Headline: large type wants a tighter line box and slightly tighter
   tracking. Body copy keeps 1.5, which is where 15px over this measure is
   comfortable. */
h1 { font-size: 22px; line-height: 1.2; letter-spacing: -.01em;
     margin: 0 0 var(--s1); }
.masthead .sub { margin-bottom: 0; }
.colophon { margin-top: var(--s5); padding-top: var(--s4);
            border-top: 1px solid var(--track); color: var(--ink-dim); }
.colophon p { margin: 0 0 var(--s1); }
.colophon a { color: var(--never); }
.sub { color: var(--ink-muted); font-size: 15px; margin-bottom: var(--s5); }
.card { background: var(--raised); border: 1px solid var(--line);
        border-radius: 10px; box-shadow: var(--lift);
        padding: var(--s3) var(--s4); margin-bottom: var(--s4); }
.banner { background: var(--warn-bg); border: 1px solid var(--warn-line);
          border-radius: 10px; box-shadow: var(--lift);
          padding: var(--s3) var(--s4); margin-bottom: var(--s4);
          color: var(--warn-ink); }
.banner ul { margin: var(--s2) 0 0 var(--s5); padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 15px; }
.scroll { overflow-x: auto; }
/* Row padding is 8px/12px rather than the old 6px/10px: this page is read at
   TV distance, and the scale has no step between them anyway. */
th, td { text-align: left; padding: var(--s2) var(--s3);
         border-bottom: 1px solid var(--line-soft); }
th { background: var(--head); position: sticky; top: 0; cursor: pointer; }
/* Zebra striping used to exist in dark mode ONLY, so the two themes read as
   different tables. One rule, one token, both modes. */
tr:nth-child(even) td { background: var(--zebra); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
"""

_CSS += """
.chart { width: 100%; height: 22px; display: block; }
.track { fill: var(--track); }
.seg-never { fill: var(--never); }
.seg-watched { fill: var(--watched); }
.seg-toonew { fill: var(--toonew); }
.seg-tuned { fill: var(--tuned); }
/* Gaps come from the x-offsets computed in _svg_split_bar, not from a
   stroke -- do not add a stroke rule here, it would double-count. */
.legend { list-style: none; display: flex; flex-wrap: wrap;
          gap: var(--s1) var(--s4);
          margin: var(--s2) 0 var(--s1); padding: 0; font-size: 15px; }
.swatch { display: inline-block; width: 11px; height: 11px; border-radius: 3px;
          margin-right: var(--s2); vertical-align: -1px; }
.swatch.seg-never { background: var(--never); }
.swatch.seg-watched { background: var(--watched); }
.swatch.seg-toonew { background: var(--toonew); }
.swatch.seg-tuned { background: var(--tuned); }
.caption { font-size: 15px; color: var(--ink-muted);
           margin: var(--s1) 0 var(--s4); }
"""

_CSS += """
.meter { width: 280px; max-width: 100%; height: 14px; vertical-align: middle; }
.meter .fill { fill: var(--never); }
/* This `opacity` is a fill blend on a decorative SVG tick, not text
   de-emphasis, so it is deliberately NOT one of the --ink-* tokens. */
.meter .tick { fill: var(--bad); opacity: .55; }
.meterrow { display: flex; flex-wrap: wrap; align-items: center;
            gap: var(--s3); margin: var(--s3) 0 var(--s4); font-size: 15px; }
/* 15px, not 14px: the type scale is 15 / 17 / 22, hand-picked and deliberately
   sparse. A 14px step sits 7% from the body size, which reads as an accident
   rather than a decision, and this page is sized for TV distance so nothing
   here shrinks. Supporting text is separated by COLOUR instead. */
.chip { font-size: 15px; padding: var(--s1) var(--s2); border-radius: 999px;
        border: 1px solid; }
/* Text stays the page's normal ink (inherited, not set here) -- #0ca30c on
   the light surface and #d03b3b on the dark surface both fall short of the
   4.5:1 that 12px text needs. Meaning is carried by the glyph + the words,
   never by colour alone, so the status hue rides on the border and the
   glyph (the <b>) only. */
.chip-ok { border-color: var(--ok); }
.chip-bad { border-color: var(--bad); }
.chip-ok b { color: var(--ok); }
.chip-bad b { color: var(--bad); }
"""

_CSS += """
.mini { width: 100px; height: 10px; vertical-align: middle; }
.mini .fill { fill: var(--never); }
td.barcell { width: 120px; }
"""

_CSS += """
details { border-top: 1px solid var(--track); padding: var(--s1) 0 var(--s2); }
/* Never add `outline: none` here. The focus ring is how this page is driven
   by a TV remote's D-pad. */
summary { font-size: 17px; font-weight: 600; cursor: pointer;
          padding: var(--s2) var(--s1); list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: '\\25B8'; display: inline-block; width: 1em;
                  color: var(--ink-dim); transition: transform .12s; }
details[open] > summary::before { transform: rotate(90deg); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
       margin-right: var(--s2); vertical-align: baseline; }
.dot-never { background: var(--never); }
.dot-watched { background: var(--watched); }
.dot-tuned { background: var(--tuned); }
.dot-toonew { background: var(--toonew); }
.dot-neutral { background: var(--track); }
/* The heading is 600; the count staying at 400 is what separates the two, so
   the number reads as data rather than as part of the label. */
.count { font-weight: 400; color: var(--ink-dim);
         font-variant-numeric: tabular-nums; }
/* Emoji ignore `color`, so this cannot be used to carry meaning even by
   accident. It is spacing only. */
.glyph { margin-right: var(--s2); }
.hint { font-size: 15px; color: var(--ink-dim); margin: 0 0 var(--s2); }
"""

# Inline, no external assets: click a header to sort its column ascending,
# click again for descending. Works with no network access (TV browsers).
_SORT_JS = """
document.querySelectorAll('table').forEach(function (table) {
  table.querySelectorAll('th').forEach(function (th, idx) {
    th.addEventListener('click', function () {
      var body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows);
      var asc = !(th.dataset.asc === 'true');
      th.dataset.asc = asc;
      rows.sort(function (a, b) {
        var x = a.cells[idx].dataset.v || a.cells[idx].textContent;
        var y = b.cells[idx].dataset.v || b.cells[idx].textContent;
        var nx = parseFloat(x), ny = parseFloat(y);
        if (!isNaN(nx) && !isNaN(ny)) { return asc ? nx - ny : ny - nx; }
        return asc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
});
"""

# Allowlisted per-channel columns only -- see the credential-safety note above.
# M1: last_watched_display renders through _fmt_local (local time, human
# text) -- the raw epoch `last_watched` field stays out of _COLUMNS and is
# only ever written to the CSV's trailing ISO-8601 columns (render_csv).
_COLUMNS = [("name", "Channel"), ("group", "Group"), ("watch_count", "Watches"),
            ("hours", "Hours"), ("avg_session_minutes", "Avg min"),
            ("tune_count", "Tunes"), ("age_days", "Age (d)"),
            ("days_since_watched", "Days since"),
            ("last_watched_display", "Last watched"), ("reason", "Reason")]


def _esc(value):
    return html_mod.escape("" if value is None else str(value))


def _fmt_local(ts):
    if not ts:
        return "n/a"
    try:
        return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "n/a"


def _table(entries):
    if not entries:
        return "<p class='sub'>None.</p>"
    head = "".join(f"<th>{_esc(label)}</th>" for _, label in _COLUMNS)
    body = []
    for entry in entries:
        cells = []
        for key, _ in _COLUMNS:
            value = entry.get(key)
            # A column may carry a separate numeric sort key alongside a
            # human display value ("never", "n/a"). Where it does, the cell
            # sorts on the number and reads as the text, and the column stays
            # right-aligned like the other numeric ones.
            sort_value = entry.get(f"{key}_sort")
            numeric = isinstance(value, (int, float)) or sort_value is not None
            cls = " class='num'" if numeric else ""
            data_v = value if sort_value is None else sort_value
            # name/group/reason are provider- or user-controlled strings and
            # get HTML-escaped like everything else routed through _esc().
            cells.append(f"<td{cls} data-v='{_esc(data_v)}'>{_esc(value)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (f"<div class='scroll'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


# Keyed on the dot class rather than the title, so a section cannot end up
# with a glyph that disagrees with its colour. Chosen to say what the section
# MEANS, not to decorate: the broken-channel section gets a warning rather than
# a sad face, because those channels want fixing rather than sympathy.
_SECTION_GLYPH = {
    "dot-never": "\N{WASTEBASKET}",             # dead weight, the turn-off list
    "dot-toonew": "\N{HOURGLASS WITH FLOWING SAND}",   # wait, do not judge yet
    "dot-tuned": "\N{WARNING SIGN}",            # probably broken, not unused
    "dot-watched": "\N{GLOWING STAR}",          # keep these
    "dot-neutral": "",                          # three different sections share
                                                # this class; no honest glyph
                                                # fits all of them, so none
                                                # gets one
}


def _section(title, count, body, open_by_default, dot_class):
    """One report section as a collapsible <details>.

    `count` is Optional[int] and None omits the span entirely, but every
    section now passes a number. For `Least used` / `Most used` that number
    is the length of the top-N slice actually rendered below, not the size
    of any wider population -- the invariant every other section obeys is
    that the count span equals the row count of the table beneath it, and
    leaving these two bare made them look like the only sections whose size
    the report would not tell you. `dot_class` is the modifier only
    ("dot-never").

    <details> needs no JavaScript, and a client that does not implement it
    renders the content EXPANDED -- the failure mode is "everything visible",
    never "content lost".
    """
    open_attr = " open" if open_by_default else ""
    number = "" if count is None else f' <span class="count">{int(count)}</span>'
    # The emoji is decoration on top of the coloured dot and the words, never
    # the only thing carrying the meaning -- same rule the palette follows, and
    # the reason it is aria-hidden. A client with no emoji font shows a box or
    # nothing, and the heading still reads correctly.
    glyph = _SECTION_GLYPH.get(dot_class, "")
    badge = (f'<span class="glyph" aria-hidden="true">{glyph}</span>'
             if glyph else "")
    return (f'<details{open_attr}><summary>'
            f'<span class="dot {_esc(dot_class)}" aria-hidden="true"></span>'
            f'{badge}{_esc(title)}{number}</summary>{body}</details>')


def render_html(model):
    """A complete, self-contained HTML page -- see the module-level note.

    Content order is a product decision, not decoration (see Task 9 brief):
    1. Never watched leads the page (with a per-group rollup first, because
       the user acts by GROUP, not channel by channel).
    2. "Too new to judge" is its own section, never merged into "Never
       watched" (R2) -- `model["never_watched"]` carries BOTH reasons so the
       CSV export sees every row, but a channel too young to fairly call
       unused (Task 8's `too_new`) is not dead weight and must not read as
       if it were: the "Never watched" section's count span must equal the
       row count of the table directly beneath it.
    3. "Tuned but never qualified" is its own section with an explanation --
       these are almost certainly BROKEN channels, not unused ones.
    4. A data-confidence header (tracking since / days / coverage%).
    5. A loud banner when gate.ok is False, listing every alert.
    """
    gate = model["gate"]
    banner = ""
    if not gate["ok"]:
        items = "".join(f"<li>{_esc(a)}</li>" for a in gate["alerts"])
        banner = (f"<div class='banner'><b>These numbers are not trustworthy yet. "
                  f"The collector may have been blind.</b><ul>{items}</ul></div>")

    rollup_rows = "".join(
        f"<tr><td>{_esc(g['group'])}</td>"
        f"<td class='num' data-v='{g['never']}'>{g['never']}</td>"
        f"<td class='num' data-v='{g['total']}'>{g['total']}</td>"
        f"<td class=\"barcell\" data-v=\"{(g['never'] / g['judged']) if g['judged'] else 0:.4f}\">"
        f"{_svg_mini_bar(g['never'], g['judged'], g['group'])}</td></tr>"
        for g in model["group_rollup"])

    counts = model["counts"]
    # R2: model['never_watched'] carries BOTH never_watched and too_new rows
    # (Task 8 keeps too_new inside the list so the CSV sees it); the heading
    # over each table must match the table it actually renders, so split here
    # rather than let the heading count and the table's row count diverge.
    never_only = [e for e in model["never_watched"] if e["reason"] != "too_new"]
    too_new_only = [e for e in model["never_watched"] if e["reason"] == "too_new"]

    # M4: "Least used" silently rendering "None." whenever len(used) <= top_n
    # is CORRECT (it keeps most_used/least_used disjoint) but baffling with
    # no explanation -- a one-line note names the reason.
    least_used_note = ""
    if counts["watched"] and not model["least_used"]:
        least_used_note = ("<p class='sub'>All watched channels are listed "
                           "above.</p>")

    # The usage rankings are drawn from the JUDGED population only, so a
    # channel that is genuinely watched but sits in an excluded group (news,
    # OTA, sports, auto-created slots) never appears in them -- on this box
    # that silently hid a third of all watched channels, Fox News and the local
    # affiliates among them. Omitting real viewing with no acknowledgement
    # makes "Most used" read as "what I watch most" when it only ever meant
    # "what I watch most among the channels I might turn off". State the gap.
    watched_excluded = sum(1 for entry in model["excluded"]
                           if (entry.get("watch_count") or 0) > 0)
    rankings_note = ""
    if watched_excluded:
        plural = "" if watched_excluded == 1 else "s"
        rankings_note = (
            f"<p class='sub'>{watched_excluded} watched channel{plural} "
            f"{'is' if watched_excluded == 1 else 'are'} <b>excluded from these "
            f"rankings</b>. These lists answer &quot;what can I turn off&quot;, "
            f"not &quot;what do I watch most&quot;. Excluded channels are "
            f"never judged, however much they are watched.</p>")

    judged = sum(counts[key] for _, key, _ in _SEGMENT_ORDER)
    bar_svg, bar_legend = _svg_split_bar(
        [(label, counts[key], css) for label, key, css in _SEGMENT_ORDER])
    caption = (f"{model['total_channels']} channels · {judged} judged · "
               f"not judged: {counts['excluded']} excluded, "
               f"{counts['unobservable']} unobservable")

    meter_svg, meter_chip = _svg_meter(model["coverage"], gate["ok"])
    meter_row = (f'<div class="meterrow">{meter_svg}'
                 f'<span>sampling density {model["coverage"]:.1%}</span>'
                 f'<span>{_esc(model["tracked_days"])} days tracked</span>'
                 f'{meter_chip}</div>')

    never_body = (
        "<div class='card'><div class='scroll'><table>"
        "<thead><tr><th>Group</th><th>Never watched</th><th>Total</th>"
        "<th>never / judged</th></tr></thead>"
        f"<tbody>{rollup_rows}</tbody></table></div></div>" + _table(never_only))

    # Every section is CLOSED by default (see EXPECTED_OPEN in the test
    # suite), so every one carries this note -- find-in-page cannot reach
    # inside a closed <details> on some browsers.
    find_hint = ("<p class='hint'>Expand to search these. Find-in-page does not "
                "reach inside a collapsed section on some browsers.</p>")

    # A conditional note can never be a section description (the description
    # guard exists because four sections once relied on notes that render only
    # on some boxes), so this sits BELOW the description, never instead of it.
    # The effective window renders unconditionally (Fix 4): a reader of a
    # report built with a ninety day window has no other way to tell whether
    # "the recent window" means seven days or ninety. The too-young case
    # (Fix 1) takes priority over both the unclamped and the clamped wording,
    # because when the dataset itself has not reached MIN_COLD_WINDOW_DAYS the
    # plugin cannot answer the cold question at all, not even with a
    # shortened window.
    if model.get("cold_window_too_young"):
        cold_note = (
            f"<p class='sub'>This dataset does not yet reach back far enough "
            f"to judge which channels have gone cold. It needs at least "
            f"{MIN_COLD_WINDOW_DAYS:.0f} days of tracked history, and "
            f"currently has {_esc(model.get('tracked_days'))}.</p>")
    elif model.get("cold_window_clamped"):
        cold_note = (f"<p class='sub'>Window shortened to "
                     f"{_esc(model.get('cold_window_days'))} days, which is as "
                     f"far back as this dataset goes.</p>")
    else:
        cold_note = (f"<p class='sub'>Recent window: "
                     f"{_esc(model.get('cold_window_days'))} days.</p>")

    # The cold classification, like the usage rankings above, only ever looks
    # at the judged population, so a watched channel sitting in an excluded
    # group can never be listed here either, however long it has gone
    # unwatched. Reuses the same count the rankings note above is built from.
    cold_excluded_note = ""
    if watched_excluded:
        plural = "" if watched_excluded == 1 else "s"
        cold_excluded_note = (
            f"<p class='sub'>{watched_excluded} watched channel{plural} "
            f"{'is' if watched_excluded == 1 else 'are'} <b>excluded from "
            f"this classification</b> too, and can never be listed as cold, "
            f"however long it has gone unwatched.</p>")

    cold_still_tried = model.get("cold_still_tried") or []
    cold_still_tried_block = ""
    if cold_still_tried:
        cold_still_tried_block = (
            "<p class='sub'>Cold by watching, but tuned recently. Somebody is "
            "still trying these and giving up before the watch threshold, so "
            "they are most likely broken rather than unwanted. Investigate "
            "these before turning any of them off.</p>"
            + _table(cold_still_tried))

    cold_abandoned = model.get("cold_abandoned") or []
    # Instead of a table, the too-young case gets a short line saying the
    # dataset does not reach back far enough yet -- there is nothing to sort
    # or search, and an empty table next to cold_note would read as "nothing
    # is cold" rather than "this cannot be judged yet".
    cold_table = ("" if model.get("cold_window_too_young")
                  else _table(cold_abandoned))

    # Every section opens with one short <p class='sub'> saying what it is and
    # what to do about it, because every section is collapsed and the summary
    # line is all the reader has to decide whether to expand. The notes that
    # follow (rankings_note, least_used_note) are CONDITIONAL, so they can
    # never be the only description a section carries.
    sections = "".join([
        _section("Never watched", counts["never_watched"],
                 "<p class='sub'>Not watched once in the tracked window. This is "
                 "the dead weight the report exists to find, and the first place "
                 "to look for something to turn off.</p>"
                 + find_hint + never_body,
                 False, "dot-never"),
        _section("Channels going cold", len(cold_abandoned) + len(cold_still_tried),
                 "<p class='sub'>Watched at some point, but not once inside the "
                 "recent window. These earned a real watch before, so they are "
                 "weaker candidates to turn off than the never watched list, "
                 "and stronger than anything below it. Sort by days since to "
                 "put the coldest first.</p>"
                 + cold_note + cold_excluded_note + find_hint + cold_table
                 + cold_still_tried_block,
                 False, "dot-neutral"),
        _section("Too new to judge", counts["too_new"],
                 "<p class='sub'>Created less than the unused threshold ago, so "
                 "not enough time has passed to fairly call these unused. Not "
                 "dead weight; just wait.</p>"
                 + find_hint + _table(too_new_only),
                 False, "dot-toonew"),
        _section("Tuned but never qualified", counts["tuned_never_qualified"],
                 "<p class='sub'>You tried to watch these and gave up quickly. They "
                 "are probably <b>broken</b> (dead source, black screen, provider "
                 "kick), not unused. Fix them rather than remove them.</p>"
                 + find_hint + _table(model["tuned_never_qualified"]),
                 False, "dot-tuned"),
        _section("Least used", len(model["least_used"]),
                 "<p class='sub'>The judged channels you watched least. They "
                 "earned a real watch, so they are weaker candidates to turn off "
                 "than the never watched list.</p>"
                 + find_hint + rankings_note + least_used_note
                 + _table(model["least_used"]),
                 False, "dot-neutral"),
        _section("Most used", len(model["most_used"]),
                 "<p class='sub'>The judged channels you watched most. Keep "
                 "these.</p>"
                 + find_hint + rankings_note + _table(model["most_used"]),
                 False, "dot-watched"),
        _section("Excluded and unobservable",
                 counts["excluded"] + counts["unobservable"],
                 "<p class='sub'>Held back from judgment by your exclusion "
                 "settings, or carried by a stream the collector cannot observe. "
                 "Neither group is ever called unused, however little it is "
                 "watched.</p>"
                 + find_hint + _table(model["excluded"] + model["unobservable"]),
                 False, "dot-neutral"),
    ])

    # The logo is optional by construction: an empty data URI renders no <img>
    # at all rather than a broken-image icon.
    logo = logo_data_uri()
    mark = (f'<img class="mark" src="{logo}" alt="" width="48" height="48">'
            if logo else "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dustarr - channel usage</title>
<style>{_CSS}</style>
</head>
<body>
<header class="masthead">
{mark}
<div>
<h1>Dustarr - channel usage</h1>
<div class="sub">
  Tracking since {_esc(_fmt_local(model['stats_since']))} ·
  {_esc(model['tracked_days'])} days ·
  coverage {model['coverage']:.1%} ·
  {model['total_channels']} channels
</div>
</div>
</header>
{bar_svg}
{bar_legend}
<div class="caption">{caption}</div>
{meter_row}
{banner}
{sections}
<footer class="colophon">
  <p>Built by Dustarr, a read only usage reporter for Dispatcharr. It records
  what you watch and never changes a channel.</p>
  <p><a href="{REPO_URL}">Source and documentation</a> ·
  <a href="{ISSUES_URL}">Report a problem</a></p>
</footer>

<script>{_SORT_JS}</script>
</body>
</html>
"""


# Excel/LibreOffice treat a cell beginning with any of these as a formula
# (CWE-1236). Provider-controlled channel/group names reach this file raw.
_FORMULA_LEAD_CHARS = ("=", "+", "-", "@")


def _csv_safe(value):
    """Neutralize CSV formula injection for a single cell.

    Only strings are touched -- watch_count/hours/tune_count/age_days are
    always int/float here, so a genuinely negative *number* is written
    unmangled. A string cell (channel name, group, reason) whose content --
    after stripping ALL leading Unicode whitespace (M9: a bare " \t" strip
    missed \r and NBSP-prefixed payloads) -- starts with =, +, -, or @ gets
    a leading single quote, the standard mitigation: Excel/LibreOffice then
    render it as literal text instead of evaluating it as a formula.
    """
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(_FORMULA_LEAD_CHARS):
        return "'" + value
    return value


def _iso_utc(ts):
    """M1: an Excel/LibreOffice user double-clicking the CSV must see a real
    date, not a raw epoch float like `1752...`. UTC (not local time) so the
    CSV is unambiguous regardless of where it's opened; `_COLUMNS`' own
    `last_watched_display` already carries the local, human-formatted form
    for the HTML report."""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def render_csv(model):
    """One row per channel across every section, deduplicated by uuid."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([key for key, _ in _COLUMNS] + ["uuid", "last_watched",
                                                    "last_tuned"])
    sections = (model["never_watched"] + model["tuned_never_qualified"]
                + model["most_used"] + model["least_used"] + model["excluded"]
                + model["unobservable"] + model["cold_abandoned"]
                + model["cold_still_tried"])
    seen = set()
    for entry in sections:
        if entry["uuid"] in seen:
            continue
        seen.add(entry["uuid"])
        row = [entry.get(key) for key, _ in _COLUMNS] + [
            entry["uuid"], _iso_utc(entry.get("last_watched")),
            _iso_utc(entry.get("last_tuned"))]
        writer.writerow([_csv_safe(cell) for cell in row])
    return buffer.getvalue()


def _atomic_write(path, text):
    # PROCESS-UNIQUE temp name. A fixed "{path}.tmp" lets two concurrent writers
    # -- the scheduled Celery run and an interactive build, or two fast clicks --
    # interleave into the SAME file and both os.replace it, publishing a TORN
    # report while `html_path` still comes back truthy, so the publish guard
    # reports it as a success.
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    try:
        os.replace(tmp, path)      # same directory: cross-device rename fails
    except OSError:
        try:
            os.remove(tmp)         # best-effort: never mask the original error
        except OSError:
            pass
        raise


ARCHIVE_KEEP = 8


def _prune_archives(dirpath, prefix, suffix, keep=ARCHIVE_KEEP):
    """Bound the report-<stamp>.* archive stream (unbounded since Phase 1;
    spec F3). Filename sort IS chronological (stamp is %Y%m%d-%H%M%S).
    Never raises; never matches the live report.html (prefix 'report-')."""
    try:
        names = sorted(n for n in os.listdir(dirpath)
                       if n.startswith(prefix) and n.endswith(suffix))
    except OSError:
        return
    for name in names[:-keep] if keep > 0 else names:
        try:
            os.remove(os.path.join(dirpath, name))
        except OSError:
            pass


def write_report(model, report_dir, csv_dir, now):
    """Write the HTML report + CSV. Never raises (M-global: I/O must degrade).

    The HTML report is the product; the CSV is a convenience export. If the
    CSV directory is unwritable, the HTML write must still succeed and be
    returned -- only the CSV path degrades to None with an "error" key
    explaining why.

    `report_dir` and `csv_dir` are now normally the SAME directory (both
    /config/dustarr), and the two calls are kept separate anyway: the archive
    pruning is keyed on the file suffix, so the two streams do not collide,
    and keeping the parameters split means a caller can still separate them.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    # There is no "url" key any more: the report is not published over HTTP.
    # `html_path` is the only handle callers get, and it is a real filesystem
    # path the operator can open.
    out = {"html_path": None, "csv_path": None, "archive_path": None}

    try:
        os.makedirs(report_dir, exist_ok=True)
        html = render_html(model)
        live = os.path.join(report_dir, REPORT_HTML)
        _atomic_write(live, html)
        # A stable filename always holds the latest run; archives sit alongside.
        archive_path = os.path.join(report_dir, f"report-{stamp}.html")
        _atomic_write(archive_path, html)
        out["html_path"] = live
        out["archive_path"] = archive_path
        _prune_archives(report_dir, "report-", ".html")
    except OSError as exc:
        out["error"] = f"html write failed: {exc}"
        return out

    try:
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, f"report-{stamp}.csv")
        _atomic_write(csv_path, render_csv(model))
        out["csv_path"] = csv_path
        _prune_archives(csv_dir, "report-", ".csv")
    except OSError as exc:
        # The HTML report is the product; a failed CSV must degrade, not raise.
        out["error"] = f"csv write failed: {exc}"

    return out
