"""Metricsarr reports -- join the ORM channel universe against usage.json.

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

import csv
import html as html_mod
import io
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
    return {
        "uuid": row.uuid,
        "name": row.name,
        "group": row.group or "(no group)",
        "watch_count": int(record.get("watch_count") or 0),
        "hours": round(float(record.get("watch_seconds") or 0.0) / 3600.0, 2),
        "last_watched": last_watched,
        # M1: "last watched 8 months ago" is the single highest-value signal
        # in the dataset for "what do I turn off" -- it was collected and
        # stored but never rendered. Formatted at entry-build time (via the
        # same _fmt_local the data-confidence header already uses) so the
        # HTML table renderer stays a generic column-driven loop.
        "last_watched_display": _fmt_local(last_watched),
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
            used.append(_entry(row, record, "watched", now))
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
                                                    "never": 0, "total": 0})
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


def summary_for_notify(model, report_url):
    return {
        "tracked_days": model["tracked_days"],
        "coverage": model["coverage"],
        "total_channels": model["total_channels"],
        "never_watched": model["counts"]["never_watched"],
        "tuned_never_qualified": model["counts"]["tuned_never_qualified"],
        "top": [{"name": c["name"], "watch_count": c["watch_count"],
                 "hours": c["hours"]} for c in model["most_used"][:5]],
        "report_url": report_url,
        "alerts": model["gate"]["alerts"],
    }


# --------------------------------------------------------------------------
# Renderers (Task 9).
#
# The report is written to nginx's already-unauthenticated static route
# (/data/logos/** is served by Dispatcharr's nginx with no auth and correct
# MIME types -- verified live) so it is one click away at
# http://<host>:9191/logos/metricsarr/report.html, instead of trapped inside
# a named Docker volume with no Windows path. The page must be fully
# self-contained (inline CSS/JS, no external assets): it is served from a
# plain static route with no build step and must render on a TV browser
# with no network access to a CDN.
#
# Credential safety: only allowlisted per-channel fields are ever rendered
# (name, uuid, group, counts, timestamps) -- never Stream.url or anything
# that could carry provider credentials, which live in stream URLs in this
# deployment.
# --------------------------------------------------------------------------

REPORT_HTML = "report.html"
REPORT_URL_PATH = "/logos/metricsarr/report.html"


def full_report_url(base_url, path=REPORT_URL_PATH):
    """I4: REPORT_URL_PATH is a bare path -- inert text in many notification
    channels, so the "link to the full report" nudge couldn't actually
    link anywhere. `base_url` is the plugin's optional `report_base_url`
    setting (the Dispatcharr UI's own base URL, e.g.
    "http://192.168.1.53:9191"); when unset, degrade to the bare path
    (today's behavior) rather than produce a broken relative-looking string.
    """
    base_url = (base_url or "").strip()
    if not base_url:
        return path
    return base_url.rstrip("/") + path

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
       margin: 0; padding: 24px; background: #fbfbfd; color: #16181d; }
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e8eaed; }
  th { background: #1e2127 !important; }
  tr:nth-child(even) td { background: #191c21; }
  .card { background: #1a1d22 !important; border-color: #2a2e35 !important; }
}
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 8px; }
.sub { opacity: .7; font-size: 13px; margin-bottom: 20px; }
.card { background: #fff; border: 1px solid #e3e5ea; border-radius: 10px;
        padding: 14px 16px; margin-bottom: 18px; }
.banner { background: #fff4e5; border: 1px solid #ffb84d; border-radius: 10px;
          padding: 12px 16px; margin-bottom: 18px; color: #7a4b00; }
.banner ul { margin: 6px 0 0 18px; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
.scroll { overflow-x: auto; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e6e8ec; }
th { background: #f2f3f6; position: sticky; top: 0; cursor: pointer; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { font-size: 12px; padding: 1px 7px; border-radius: 999px;
        background: #eceef2; }
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
            ("hours", "Hours"), ("tune_count", "Tunes"), ("age_days", "Age (d)"),
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
            numeric = isinstance(value, (int, float))
            cls = " class='num'" if numeric else ""
            # name/group/reason are provider- or user-controlled strings and
            # get HTML-escaped like everything else routed through _esc().
            cells.append(f"<td{cls} data-v='{_esc(value)}'>{_esc(value)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (f"<div class='scroll'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def render_html(model):
    """A complete, self-contained HTML page -- see the module-level note.

    Content order is a product decision, not decoration (see Task 9 brief):
    1. Never watched leads the page (with a per-group rollup first, because
       the user acts by GROUP, not channel by channel).
    2. "Too new to judge" is its own section, never merged into "Never
       watched" (R2) -- `model["never_watched"]` carries BOTH reasons so the
       CSV export sees every row, but a channel too young to fairly call
       unused (Task 8's `too_new`) is not dead weight and must not read as
       if it were: the "Never watched (N)" heading's N must equal the row
       count of the table directly beneath it.
    3. "Tuned but never qualified" is its own section with an explanation --
       these are almost certainly BROKEN channels, not unused ones.
    4. A data-confidence header (tracking since / days / coverage%).
    5. A loud banner when gate.ok is False, listing every alert.
    """
    gate = model["gate"]
    banner = ""
    if not gate["ok"]:
        items = "".join(f"<li>{_esc(a)}</li>" for a in gate["alerts"])
        banner = (f"<div class='banner'><b>These numbers are not trustworthy yet"
                  f" -- the collector may have been blind.</b><ul>{items}</ul></div>")

    rollup_rows = "".join(
        f"<tr><td>{_esc(g['group'])}</td>"
        f"<td class='num' data-v='{g['never']}'>{g['never']}</td>"
        f"<td class='num' data-v='{g['total']}'>{g['total']}</td></tr>"
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

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Metricsarr - channel usage</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Metricsarr - channel usage</h1>
<div class="sub">
  Tracking since {_esc(_fmt_local(model['stats_since']))} ·
  {_esc(model['tracked_days'])} days ·
  coverage {model['coverage']:.1%} ·
  {model['total_channels']} channels ·
  <span class="pill">{counts['never_watched']} never watched</span>
  <span class="pill">{counts['watched']} watched</span>
  <span class="pill">{counts['excluded']} excluded</span>
</div>
{banner}

<h2>Never watched ({counts['never_watched']})</h2>
<div class="card">
  <div class="scroll"><table>
    <thead><tr><th>Group</th><th>Never watched</th><th>Total</th></tr></thead>
    <tbody>{rollup_rows}</tbody>
  </table></div>
</div>
{_table(never_only)}

<h2>Too new to judge ({counts['too_new']})</h2>
<p class="sub">Created less than the unused threshold ago -- not enough time
has passed to fairly call these unused. Not dead weight; just wait.</p>
{_table(too_new_only)}

<h2>Tuned but never qualified ({counts['tuned_never_qualified']})</h2>
<p class="sub">You tried to watch these and gave up quickly. They are probably
<b>broken</b> (dead source, black screen, provider kick), not unused.</p>
{_table(model['tuned_never_qualified'])}

<h2>Least used</h2>
{least_used_note}
{_table(model['least_used'])}

<h2>Most used</h2>
{_table(model['most_used'])}

<h2>Excluded ({counts['excluded']}) and unobservable ({counts['unobservable']})</h2>
{_table(model['excluded'] + model['unobservable'])}

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
                + model["unobservable"])
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

    The HTML report is the product; the CSV is a convenience export to a real
    bind mount. If the CSV directory is unwritable, the HTML write must still
    succeed and be returned -- only the CSV path degrades to None with an
    "error" key explaining why.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    out = {"html_path": None, "csv_path": None, "archive_path": None,
           "url": REPORT_URL_PATH}

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
