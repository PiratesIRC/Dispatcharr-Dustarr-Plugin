"""Dustarr — channel usage metrics for Dispatcharr. READ-ONLY.

Phase 1 mutates NOTHING in Dispatcharr: it polls Redis, writes its own files, and
reports. Spec: docs/superpowers/specs/2026-07-12-*-design.md (filed before the rename).

Plugin.__init__ is O(ms) and I/O-free. (The procfs read that gates the collector
to uWSGI workers lives in ensure_collector, not here.) Django imports are
function-local -- a module-level Django import breaks the loader. celery is the
one exception: it must be imported at module scope so @shared_task actually
registers build_report_task (C1) -- celery is available in the Dispatcharr
runtime, same as every sibling plugin's Celery task.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time

from celery import shared_task

try:
    from . import collector as collector_mod
    from . import gates, gateway, notify_report, redaction, reports, sessionizer, storage
except ImportError:                     # standalone (non-package) import path
    import collector as collector_mod
    import gates
    import gateway
    import notify_report
    import redaction
    import reports
    import sessionizer
    import storage

_LOGGER = logging.getLogger(__name__)

PLUGIN_VERSION = "1.26.2251625"

DATA_DIR = "/data/dustarr"           # plugin state (named volume)
# Both outputs go to the SAME directory, under Dispatcharr's existing /config
# bind mount, so they land in a real folder on the host that the operator can
# open directly. The HTML report used to live in /data/logos/dustarr, which
# Dispatcharr's nginx serves to the whole LAN with no authentication -- an
# unauthenticated listing of every channel this household watches. Do not move
# it back.
REPORT_DIR = "/config/dustarr"       # bind-mounted, so reachable from the host
CSV_DIR = "/config/dustarr"          # same directory; suffix keeps the archives apart

THREAD_NAME = "dustarr-collector"
TASK_NAME = "dustarr_build_report"
# Dispatcharr imports a plugin as `_dispatcharr_plugin_<package dir>`, so this
# path is only correct while the package directory is named `dustarr`. A wrong
# path fails INVISIBLY: Beat keeps counting dispatches it never executed.
# tests/test_plugin_contract.py binds it to the real directory name.
TASK_PATH = "_dispatcharr_plugin_dustarr.plugin.build_report_task"
# bug-075: plugin @shared_tasks register ONLY on the threads `dvr` worker,
# never on the prefork `celery` worker. A row without this queue is rejected.
SCHEDULE_QUEUE = "dvr"
RESTART_BOUND = 5
RESTART_WINDOW_S = 3600.0

# The metadata key's TTL is 30s (refreshed every 1s). A poll slower than this can
# miss a live channel between refreshes entirely (fact-check #3).
MAX_POLL_INTERVAL_S = 25

# I5: the collector loop's outer except has NO OTHER observability -- rate
# limit its one log line so a tight failure loop cannot flood the log.
_ERROR_LOG_INTERVAL_S = 60.0

_restart_times = []
_spawn_lock = threading.Lock()

FIELDS = [
    # Orientation panel, FIRST field (operator decision 2026-07-26). Mirrors
    # EPG-Janitor's `_section_quickstart`. Deliberately ONE flowing paragraph
    # with the steps inline: the reference implementation does not risk
    # multi-line layout in an info body, and the action `message` toast is
    # known to collapse newlines. No em dashes, per operator instruction.
    {"id": "_section_quickstart", "type": "info", "label": "Quick Start",
     "description": "New here? The buttons are in the order you want to press "
                    "them: 1) Validate settings checks your config, the "
                    "collector, the schedule and whether email can actually go "
                    "out. 2) Show summary prints the headline numbers without "
                    "writing anything. 3) Build report writes the HTML report "
                    "plus the CSV export into the config folder, and also "
                    "emails it if Send notifications to Newsflasharr is on, "
                    "which needs Newsflasharr installed and enabled with its "
                    "SMTP set up and a routing rule pointing dustarr at smtp. "
                    "The report is written either way, and the button names "
                    "anything that stopped the mail. Report an issue shows the "
                    "link to the issue tracker. Expect a "
                    "'not trustworthy' banner for the first 30 days: the "
                    "dataset has to be older than the unused threshold before "
                    "anything can fairly be called unused, so that banner is "
                    "the age gate working, not a fault."},
    {"id": "info", "type": "info", "label": "Dustarr is read-only",
     "description": "It records which channels are watched and reports the dead "
                    "weight. It never changes a channel."},
    {"id": "poll_interval_s", "label": "Poll interval (s)", "type": "number",
     "default": 15, "min": 5, "max": 25,
     "description": "Sampling cadence. Must stay under Dispatcharr's 30s metadata "
                    "TTL or live channels are missed between refreshes."},
    {"id": "min_watch_seconds", "label": "Minimum watch (s)", "type": "number",
     "default": 120,
     "description": "Shorter than this is a channel-surf, not a watch. It still "
                    "counts as a 'tune' and appears in the report."},
    {"id": "client_gap_grace_s", "label": "Client gap grace (s)", "type": "number",
     "default": 90,
     "description": "How long a player may drop to zero clients (a retry) before "
                    "the session is considered over. Retry gaps exceed 40s."},
    {"id": "merge_gap_s", "label": "Session merge gap (s)", "type": "number",
     "default": 120,
     "description": "A channel re-tuned within this window continues the same "
                    "watch instead of starting a new one."},
    {"id": "unused_threshold_days", "label": "Unused threshold (days)",
     "type": "number", "default": 30,
     "description": "A channel younger than this cannot be judged unused."},
    {"id": "recent_window_days", "label": "Cold threshold (days)",
     "type": "number", "default": 30, "min": 7, "max": 3650,
     "description": "How long an absence counts as cold. A channel watched at "
                    "some point, but not once inside this window, is listed as "
                    "going cold."},
    {"id": "top_n", "label": "Top/bottom N", "type": "number", "default": 20},
    {"id": "never_watched_ceiling", "label": "Never-watched alarm ceiling",
     "type": "number", "default": 0.98,
     "description": "Fraction of JUDGED channels (never-watched + too-new + "
                    "tuned-but-never-qualified + watched -- excluded/"
                    "unobservable channels don't count) that must look "
                    "never-watched before the data is treated as untrustworthy "
                    "(a blind collector). Most real lineups exclude most "
                    "channels by policy, so a healthy household can easily "
                    "show 80-90% never-watched among the rest; this high "
                    "default only catches the mass-casualty shape where "
                    "essentially EVERY judged channel looks dead."},
    {"id": "exclude_auto_created", "label": "Exclude auto-created channels",
     "type": "boolean", "default": True,
     "description": "Protects PPV/LIVE EVENT slots and 24/7 channels, which M3U "
                    "sync renames in place and which idle between events."},
    {"id": "exclude_groups", "label": "Excluded groups (comma separated)",
     "type": "text", "default": gateway.DEFAULT_EXCLUDE_GROUPS,
     "description": "Never judged unused. Local/OTA/news is the emergency tier; "
                    "sports has a legitimate off-season."},
    {"id": "exclude_name_regex", "label": "Excluded name regex", "type": "string",
     "default": gateway.DEFAULT_EXCLUDE_NAME_RE},
    {"id": "notify_enabled", "label": "Send notifications to Newsflasharr",
     "type": "boolean", "default": False,
     "help_text": "Requires the Newsflasharr plugin. What routes where is "
                  "configured in Newsflasharr's routing rules, keyed on this "
                  "plugin's name."},
    {"id": "report_schedule", "label": "Scheduled report", "type": "select",
     "default": "weekly",
     "options": [{"value": "off", "label": "Off"},
                 {"value": "daily", "label": "Daily (03:00)"},
                 {"value": "weekly", "label": "Weekly (Mon 03:00)"},
                 {"value": "monthly", "label": "Monthly (1st, 03:00)"}]},
]

ISSUES_URL = "https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/issues"

# Validate settings is FIRST (operator decision 2026-07-27): it is the button
# to press before any other, it writes nothing, and it is what diagnoses the
# schedule. The order here matches the Quick Start copy, and a test binds the
# two together.
ACTIONS = [
    {"id": "validate_settings", "label": "Validate settings",
     "description": "Check every setting parses, and report on the collector, "
                    "the schedule and email readiness. Writes nothing.",
     "button_label": "✅ Validate",
     "button_variant": "outline", "button_color": "green"},
    {"id": "show_summary", "label": "Show summary",
     "description": "Tracking window, coverage, never-watched count.",
     "button_label": "📊 Summary",
     "button_variant": "outline", "button_color": "blue"},
    {"id": "build_report", "label": "Build report",
     "description": "Write the HTML report and CSV now, and email it with the "
                    "file attached if notifications are on. This is the same "
                    "job the schedule runs. With notifications off it simply "
                    "writes the files and says so. With them on it needs "
                    "Newsflasharr installed and enabled, with its SMTP "
                    "configured and a routing rule sending dustarr to smtp, "
                    "and it names anything missing. The report is written "
                    "either way. Does NOT prove the SCHEDULE works: this runs "
                    "in the web worker, the schedule runs on a Celery worker.",
     "button_label": "📈 Build report",
     "button_variant": "filled", "button_color": "orange"},
    {"id": "report_issue", "label": "Report an issue",
     "description": "Show the link to this plugin's GitHub issue tracker. A "
                    "plugin action cannot open a browser tab, so it prints the "
                    "address for you to copy.",
     "button_label": "🐞 Report an issue",
     "button_variant": "outline", "button_color": "gray"},
]

# recent_window_days floors at 7, not 1: a channel that has been streaming
# continuously for longer than the window, and whose last completed watch
# also predates the window, fails both timestamp tests (see
# sessionizer.py: last_watched is written only when a session finalizes,
# and last_tuned only when a session opens) and is listed as abandoned while
# it is actually on screen. It does not even reach the still-tried list,
# because the tune timestamp is equally stale for one unbroken session. At a
# window of 1 day an always-on television reaches that state in 24 hours. A
# floor of 7 days makes it require an implausible unbroken session.
_NUMERIC_FLOORS = {"poll_interval_s": (5, MAX_POLL_INTERVAL_S),
                   "min_watch_seconds": (10, 3600),
                   "client_gap_grace_s": (30, 600),
                   "merge_gap_s": (0, 600),
                   "unused_threshold_days": (1, 3650),
                   "recent_window_days": (7, 3650),
                   "top_n": (1, 500),
                   "never_watched_ceiling": (0.05, 1.0)}


def coerce_settings(settings):
    """Settings arrive UNVALIDATED from the API: coerce and floor everything."""
    settings = settings if isinstance(settings, dict) else {}
    out = {}
    for field in FIELDS:
        fid = field["id"]
        if field["type"] == "info":
            continue
        default = field.get("default")
        value = settings.get(fid, default)
        if field["type"] == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(default)
            if not math.isfinite(value):
                # NaN/inf must fall back to the field default, not clamp to
                # whatever floor/ceiling happens to be nearest (M2) -- e.g.
                # unused_threshold_days=inf must NOT become 3650 (meaning
                # nothing is EVER judged unused).
                value = float(default)
            low, high = _NUMERIC_FLOORS.get(fid, (None, None))
            if low is not None:
                value = max(low, min(high, value))
            if fid in ("top_n", "unused_threshold_days", "recent_window_days"):
                value = int(value)
            elif fid == "poll_interval_s" and value == int(value):
                value = int(value)
        elif field["type"] == "boolean":
            # Settings arrive UNVALIDATED (module docstring above): a boolean
            # field can reach here as the string "false" from a form post or
            # an API client, and bool("false") is True (any non-empty string
            # is truthy) -- exactly the silent-toggle-never-works shape this
            # plugin's own history keeps producing. notify_enabled in
            # particular must resolve to a REAL bool for a later task's
            # `coerce_settings(settings).get("notify_enabled")` gate to work.
            if isinstance(value, str):
                value = value.strip().lower() not in ("", "false", "0", "no")
            else:
                value = bool(value)
        elif field["type"] == "select":
            options = {opt["value"] for opt in field.get("options", [])}
            if value not in options:
                # An unrecognized select value (e.g. report_schedule="hourly")
                # must fall back to the field default, not flow through to
                # sync_schedule's CRON_BY_SCHEDULE.get() miss -> silently OFF
                # scheduling (I4).
                value = default
        out[fid] = value
    return out


def _get_redis():
    from core.utils import RedisClient  # Dispatcharr runtime only
    return RedisClient.get_client()


def _gateway():
    return gateway.DjangoGateway()


def _is_uwsgi_worker():
    try:
        with open("/proc/self/cmdline", "rb") as fh:
            return b"uwsgi" in fh.read().lower()
    except OSError:
        return False


# ---- collector thread -------------------------------------------------------
# The fingerprint covers only the settings the collector actually reads,
# derived from sessionizer.DEFAULTS -- the poll interval, the minimum watch
# length, the client gap grace and the session merge gap are the whole set
# collector.py and sessionizer.py consult. Deriving the key set from that
# dictionary, rather than listing the four names again here, means a setting
# that does not affect collection can never trigger a respawn (every other
# setting -- top/bottom N, the unused threshold, the alarm ceiling, the
# notification toggle, the report schedule, the exclusion settings -- is
# report-only and must not forfeit an in-flight watch session), and a new
# collection threshold can never be forgotten, because a threshold absent from
# sessionizer.DEFAULTS is, by construction, one the collector does not use.
_COLLECTOR_SETTING_KEYS = frozenset(sessionizer.DEFAULTS)


def _thresholds_fingerprint(thresholds):
    """A hashable snapshot of coerced settings (I1), used to detect a settings
    change that must respawn the collector even when PLUGIN_VERSION hasn't
    moved. `thresholds` is always coerce_settings()'s output -- only numbers/
    bools/strings -- so a sorted tuple of items is stable and comparable.
    Includes only _COLLECTOR_SETTING_KEYS, the settings the collector reads."""
    return tuple(sorted((key, value) for key, value in thresholds.items()
                        if key in _COLLECTOR_SETTING_KEYS))


def _spawn_collector(settings):
    stop_event = threading.Event()
    thresholds = coerce_settings(settings)

    def loop():
        import uuid as uuid_mod

        token = f"{os.getpid()}:{uuid_mod.uuid4().hex}:{PLUGIN_VERSION}"
        sess = sessionizer.Sessionizer(thresholds)
        store = storage.Storage(DATA_DIR)
        col = None
        backoff = 5.0
        last_error_log = 0.0
        while not stop_event.is_set():
            wait = float(thresholds["poll_interval_s"])
            try:
                if col is None:
                    col = collector_mod.Collector(
                        _get_redis(), sess, store, thresholds,
                        token=token, wall=time.time)
                col.run_tick()
                backoff = 5.0
                wait = col.base_tick()
            except Exception:
                wait = backoff
                backoff = min(backoff * 2, 300.0)   # Redis-outage backoff
                # I5: this is the ONLY observability this thread has. An
                # exception escaping run_tick entirely (e.g. Collector
                # construction itself failing) never touches run_tick's own
                # stats["last_error"] -- that's set only inside run_tick's
                # OWN try/except -- and stats never reach disk if run_tick
                # keeps raising (usage.json is written by _flush, which
                # never runs). Without this log line a persistently-broken
                # collector is completely invisible: no logs, no usage.json,
                # no self-health. Rate-limited so a tight failure loop
                # cannot flood the log.
                now_log = time.time()
                if now_log - last_error_log >= _ERROR_LOG_INTERVAL_S:
                    _LOGGER.exception("dustarr collector tick failed")
                    last_error_log = now_log
            if stop_event.wait(wait):
                break
        if col is not None:
            col.shutdown()

    thread = threading.Thread(target=loop, name=THREAD_NAME, daemon=True)
    thread.dustarr_version = PLUGIN_VERSION
    thread.dustarr_fingerprint = _thresholds_fingerprint(thresholds)
    thread.dustarr_stop = stop_event
    thread.start()
    return thread


def ensure_collector(settings=None):
    """Idempotent: one live collector thread per worker, superseded on a
    version bump OR a settings change (I1).

    Thresholds are frozen into the collector loop's closure at spawn time, so
    a live thread whose settings have since changed (e.g. poll_interval_s
    lowered 15 -> 5) would otherwise keep polling at the OLD cadence forever
    while the report layer reads settings fresh every run -- silently
    poisoning gates.coverage_fraction's `needed` computation and reading 0.0
    coverage forever. Keying supersession on (PLUGIN_VERSION, thresholds
    fingerprint) together, instead of version alone, closes that gap.
    """
    if not _is_uwsgi_worker():
        return
    fingerprint = _thresholds_fingerprint(coerce_settings(settings or {}))
    with _spawn_lock:
        live = [t for t in threading.enumerate()
                if t.name == THREAD_NAME and t.is_alive()]
        for thread in live:
            if (getattr(thread, "dustarr_version", None) == PLUGIN_VERSION
                    and getattr(thread, "dustarr_fingerprint", None) == fingerprint):
                return

        # M3 / I1: check the crash-loop bound BEFORE stopping any incumbent
        # thread, and for EITHER kind of supersession (version bump or
        # settings change) -- both share one budget, so a burst of settings
        # edits cannot bypass the same thrash guard a burst of version bumps
        # would hit. Checking it after stopping the incumbent (the old order)
        # meant a 6th supersession within the window would kill the running
        # collector and then refuse to spawn its replacement, leaving the
        # worker with NO collector at all.
        now = time.time()
        _restart_times[:] = [t for t in _restart_times if now - t < RESTART_WINDOW_S]
        if len(_restart_times) >= RESTART_BOUND:
            return                          # crash-loop bound; incumbent survives

        for thread in live:
            getattr(thread, "dustarr_stop", threading.Event()).set()

        _restart_times.append(now)
        _spawn_collector(settings or {})


# ---- report ------------------------------------------------------------------
def _build_report(settings):
    thresholds = coerce_settings(settings)
    store = storage.Storage(DATA_DIR)
    gw = _gateway()
    now = gw.now()

    usage = store.load(now)
    rows = gw.channels()
    model = reports.build_model(rows, usage, thresholds, now)
    written = reports.write_report(model, REPORT_DIR, CSV_DIR, now)

    counts = model["counts"]
    message = (f"{counts['never_watched']} of {model['total_channels']} channels "
               f"never watched ({model['tracked_days']}d tracked, "
               f"coverage {model['coverage']:.0%}).")
    if not model["gate"]["ok"]:
        message += f" NOT TRUSTWORTHY: {model['gate']['alerts'][0]}"

    # bug-078: the counts above are computed BEFORE the write, and write_report
    # never raises (it degrades, by design), so a hardcoded "ok" here reported a
    # perfectly healthy-looking summary for a run that published NOTHING -- the
    # live symptom when the report directory was root-owned and the Celery
    # worker runs as `dispatch`. The only evidence was report.html's unmoved
    # mtime. Status must reflect the PUBLISH, not the computation.
    #
    # write_report returns early on an HTML failure, so a falsy html_path is the
    # reliable "nothing was published" signal. A CSV-only failure stays a
    # degraded success on purpose: the HTML report is the product, the CSV is a
    # convenience export.
    published = bool(written.get("html_path"))
    html_path = written.get("html_path")
    # Counted here and nowhere else, and ONLY on a confirmed publish, so the
    # number means "reports that exist" rather than "times the button was
    # pressed". Both entry points (the action and the Celery task) reach this
    # function, so both count.
    if published:
        written["reports_built"] = bump_report_count()
    # A filesystem path, never a URL: the report is not served over HTTP. The
    # directory is bind-mounted, so this path is openable from Windows.
    where = html_path or REPORT_DIR
    result = {"status": "ok" if published else "error",
              "message": message + f" Report: {where}",
              "file": where}
    if written.get("error"):
        result["message"] += f" ({written['error']})"
    if not published:
        # bug-078 shipped this guard reporting only `status`, which the plugin
        # card does not render -- so "nothing was published" looked identical to
        # success. `error` is the only persistent, red surface.
        result["error"] = (f"Report was NOT published: "
                           f"{written.get('error') or 'unknown write failure'}")
    return result, model, written


def _notify_client():
    """The vendored Newsflasharr caller client -- lazy-imported (module scope
    would break the loader the same way a top-level Django import does) and
    resolved via a helper so tests can monkeypatch it in one place."""
    try:
        from . import notify_client
        return notify_client
    except ImportError:
        import notify_client
        return notify_client


def _emit_notifications(settings, model, written):
    """Newsflasharr emits. The single writer for the honesty-gate state
    (notify_state.json). The interactive `build_report` action deliberately
    does NOT emit (one report per run, not one per click).

    Never raises: a notify failure -- or Newsflasharr not being installed at
    all -- must never fail the report task. Must run AFTER the caller has
    confirmed written["html_path"] is truthy (bug-078's lesson one layer up:
    a report that was never published must not still trigger a notification
    about it).

    Returns `{"enabled": bool, "report_emitted": bool, "error": str|None}`.
    It used to return None and discard `emit_report`'s bool, so a REFUSED
    spool write -- `notify()` never raises, it returns False -- left the run
    reporting green with no trace anywhere. The dead-signal shape this
    codebase keeps producing. `error` carries a redacted reason, never a raw
    exception: provider credentials live inside stream URLs here.
    """
    result = {"enabled": False, "report_emitted": False, "error": None}
    try:
        thresholds = coerce_settings(settings)
        if not thresholds.get("notify_enabled"):
            return result
        result["enabled"] = True
        nc = _notify_client()
        archive = written.get("archive_path")
        summary = reports.summary_for_notify(model, written.get("reports_built"))
        # `url=None`, always: the report is not published over HTTP, so there
        # is no link to send. The report reaches the operator as the emailed
        # ATTACHMENT (`archive`) and as a file on the bind mount.
        emitted, why = notify_report.emit_report_result(
            nc.notify, summary, None, archive)
        result["report_emitted"] = bool(emitted)
        if not emitted:
            # `error` was previously set ONLY by this function's own except,
            # while the False return comes from notify() returning bare False --
            # so a realistic refusal carried no cause at all.
            result["error"] = why

        state_path = os.path.join(DATA_DIR, notify_report.STATE_FILE)
        prev_ok = notify_report.load_prev_ok(state_path)
        new_ok, _action = notify_report.emit_gate(
            nc.notify, model, thresholds, prev_ok)
        # Avoid a pointless rewrite every single build when the gate state
        # hasn't actually changed -- only persist on a real transition, or
        # when the state file doesn't exist yet at all.
        save_needed = new_ok != prev_ok or not os.path.exists(state_path)
        if save_needed:
            notify_report.save_prev_ok(state_path, new_ok)
    except Exception as exc:
        result["error"] = redaction.redact(f"{exc}")
        _LOGGER.warning("dustarr notify emit failed (suppressed)")
    return result


@shared_task
def build_report_task():
    """Celery entry point. Runs on the PREFORK queue -- real processes, no gevent,
    so ORM-heavy work here cannot wedge a uWSGI worker (bug-117).

    @shared_task is REQUIRED, not decorative (C1): Dispatcharr's worker_ready
    hook eagerly imports plugins so their @shared_task functions register --
    the import alone does not register anything. Without the decorator, Beat
    fires this task's name on schedule forever and the worker rejects every
    run with "Received unregistered task".

    The whole body is wrapped (I2): an uncaught exception here propagates raw
    into the Celery log, and provider credentials live inside stream URLs in
    this deployment -- so a failure must be logged redacted, never raw. The
    redacted message is then RE-RAISED (not swallowed into a `{"error": True}`
    return): Celery's result backend records a return value as SUCCESS
    regardless of its contents, so swallowing the exception made a failed
    scheduled report show green and unretryable forever. Re-raising preserves
    both guarantees at once -- the credential redaction AND Celery's normal
    failure/retry semantics.

    Raised `from None` (I6) -- NOT `from exc`. `from exc` sets `__cause__` to
    the ORIGINAL exception, so Celery's stored/logged traceback renders the
    credential-bearing original verbatim even though the message string is
    clean (`ValueError: ...topsecretpass.../ The above exception was the
    direct cause of the following exception: RuntimeError: ...<redacted>...`).
    A bare `raise RuntimeError(redacted)` inside this except block isn't safe
    either: Python still sets `__context__` to the exception being handled
    (implicit chaining), which the traceback formatter also renders unless
    told not to. `from None` is the only form that suppresses BOTH.

    bug-078 is the SAME shape one layer down, and needed its own fix: a failed
    WRITE is never an exception at all (write_report degrades by design), and
    the counts are computed before the write, so the first-ever scheduled run
    returned SUCCESS with a full healthy-looking payload while publishing
    nothing -- /data/logos/dustarr was root-owned and this worker runs as
    `dispatch`. A green Celery result does not prove a report exists; only a
    non-empty html_path does. Hence the explicit raise below.
    """
    from django.db import close_old_connections

    try:
        close_old_connections()
        settings = _load_settings()
        _, model, written = _build_report(settings)
        if not written.get("html_path"):
            # bug-078: nothing was published, so returning counts here would
            # record SUCCESS for a run that wrote no files. Raised INSIDE the
            # try so the except block below redacts it and re-raises `from
            # None`, keeping both guarantees intact.
            raise RuntimeError(
                f"report not published: {written.get('error') or 'unknown write failure'}")
        _emit_notifications(settings, model, written)
        # AFTER the publish guard above, so a run that published nothing is
        # never recorded as a healthy scheduled run (bug-078's lesson).
        _write_scheduled_run_ts(time.time())
        return model["counts"]
    except Exception as exc:
        redacted = redaction.redact(str(exc))
        _LOGGER.error("dustarr build_report_task failed: %s", redacted)
        raise RuntimeError(redacted) from None


def _load_settings():
    from apps.plugins.models import PluginConfig  # Dispatcharr runtime only

    try:
        config = PluginConfig.objects.get(key="dustarr")
        return config.settings or {}
    except Exception:
        return {}


CRON_BY_SCHEDULE = {"daily": "0 3 * * *", "weekly": "0 3 * * 1",
                    "monthly": "0 3 1 * *"}


SCHEDULED_RUN_FILE = "scheduled_run.json"


def _scheduled_run_path():
    return os.path.join(DATA_DIR, SCHEDULED_RUN_FILE)


def _write_scheduled_run_ts(now):
    """Record that the SCHEDULE ran. Written by build_report_task ONLY.

    Nothing else can answer the question. Beat's `total_run_count` counts
    messages SENT, not executed, so it reads healthy for a task the worker
    rejects -- which is exactly the outage found on 2026-07-25. And
    Newsflasharr's `last_attachment_delivered_ts` is provenance-blind: it
    stamps on ANY successful smtp send carrying a file, so a manual send
    satisfies it identically and would mask a dead scheduler indefinitely.

    Never raises: a health signal must not break the run it reports on.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _scheduled_run_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last_scheduled_run_ts": float(now)}, fh)
        os.replace(tmp, _scheduled_run_path())
    except Exception:
        _LOGGER.warning("dustarr could not record the scheduled run")


def _read_scheduled_run_ts():
    """-> float, or None for never/unreadable/corrupt.

    Degrades to "never ran", which is the SAFE direction: this file exists to
    REPORT a problem, so an unreadable one must not hide one.
    """
    try:
        with open(_scheduled_run_path(), encoding="utf-8") as fh:
            value = json.load(fh).get("last_scheduled_run_ts")
        return float(value) if value is not None else None
    except Exception:
        return None


REPORT_COUNT_FILE = "report_count.json"


def _report_count_path():
    return os.path.join(DATA_DIR, REPORT_COUNT_FILE)


def read_report_count():
    """-> int, the number of reports this plugin has successfully PUBLISHED.

    Degrades to 0 on a missing, unreadable or corrupt file, and on a negative
    or non-numeric stored value. A counter is a cosmetic signal; it must never
    be the reason a report fails to build.

    Zero is the SAFE degradation here, which is the opposite of
    `_read_scheduled_run_ts` above and worth stating so the asymmetry does not
    read as an oversight. That function reports a PROBLEM, so an unreadable
    file must not hide one and it degrades toward "never ran". This one feeds a
    badge, so an unreadable file must not INVENT activity.
    """
    try:
        with open(_report_count_path(), encoding="utf-8") as fh:
            value = int(json.load(fh).get("reports_built", 0))
        return value if value >= 0 else 0
    except Exception:
        return 0


def bump_report_count():
    """Increment the published-report counter and return the new value.

    Call this ONLY after the HTML file is confirmed on disk (a truthy
    `html_path`). `write_report` degrades rather than raising, so counting
    before that check would count reports that do not exist -- the same shape
    as bug-078, where a run that published nothing returned a healthy summary.

    Never raises: on any failure the caller gets the value it would have had,
    so a broken counter costs a badge increment and nothing else.

    KNOWN LIMIT, deliberately not solved: this is a read-modify-write with no
    lock, and Dispatcharr runs several uWSGI and Celery workers. Two reports
    finishing in the same instant can lose one increment. A lock spanning file
    I/O on the request path is a worse trade than an occasional undercount in
    a cosmetic number, and reports are minutes apart in practice.
    """
    current = read_report_count()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _report_count_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"reports_built": current + 1}, fh)
        os.replace(tmp, _report_count_path())
        return current + 1
    except Exception:
        return current


NOTIFY_SOURCE = "dustarr"
NOTIFY_EVENT = "usage_report"
NEWSFLASHARR_KEY = "newsflasharr"
_SMTP_REQUIRED = ("smtp_server", "smtp_username", "smtp_password", "smtp_to")


def _routes_to_smtp(nf_settings):
    """Would a dustarr usage_report actually reach the smtp channel?

    `routing_rules` is stored as a JSON STRING, not a list, so it is parsed
    defensively; a list is accepted too in case that ever changes. A rule with
    no `source` or no `event` is a wildcard and matches. If nothing matches,
    the event falls through to `default_channels`, which is the silent failure
    this check exists to catch: delivery looks fine and the mail goes elsewhere.
    """
    raw = nf_settings.get("routing_rules")
    rules = raw if isinstance(raw, list) else []
    if isinstance(raw, str):
        try:
            rules = json.loads(raw)
        except (ValueError, TypeError):
            rules = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
        if match.get("source") not in (None, NOTIFY_SOURCE):
            continue
        if match.get("event") not in (None, NOTIFY_EVENT):
            continue
        channels = rule.get("channels")
        if any("smtp" in str(c).lower() for c in (channels or [])):
            return True
    return "smtp" in str(nf_settings.get("default_channels") or "").lower()


def _newsflasharr_readiness():
    """Everything that must be true for an emailed report to actually arrive.

    Read-only on another plugin's configuration row, which is explicitly
    allowed; nothing here writes. Returns a list of blocking problems, empty
    when the path is clear.

    This runs BEFORE the report is built. Every one of these failures used to
    surface only afterwards, or not at all: a missing routing rule in
    particular is invisible, because spooling succeeds and the event is simply
    delivered somewhere other than the inbox.

    Never echo a settings VALUE here. `smtp_password` is one of the keys being
    checked and only its presence is ever reported.
    """
    try:
        from apps.plugins.models import PluginConfig  # Dispatcharr runtime only
    except Exception:
        return ["Cannot reach Dispatcharr's plugin registry to check "
                "Newsflasharr."]
    try:
        row = PluginConfig.objects.filter(key=NEWSFLASHARR_KEY).first()
    except Exception as exc:
        return ["Could not read Newsflasharr's configuration: "
                f"{redaction.redact(str(exc))}"]

    if row is None:
        return ["Newsflasharr is not installed, and it is what actually sends "
                "the mail."]

    problems = []
    if not getattr(row, "enabled", False):
        problems.append("Newsflasharr is installed but not enabled.")

    nf_settings = row.settings if isinstance(getattr(row, "settings", None), dict) else {}
    missing = [key for key in _SMTP_REQUIRED
               if not str(nf_settings.get(key) or "").strip()]
    if missing:
        problems.append("Newsflasharr's SMTP is not fully configured (missing: "
                        + ", ".join(missing) + ").")
    elif not _routes_to_smtp(nf_settings):
        problems.append(
            f"Newsflasharr has no routing rule sending {NOTIFY_SOURCE}'s "
            f"{NOTIFY_EVENT} to smtp, and smtp is not among its default "
            "channels, so the report would be delivered somewhere else.")
    return problems


def _notifier_alive():
    """Is Newsflasharr's collector ticking?

    `notify()` returns True for a successful SPOOL WRITE and CREATES the spool
    directory itself, so it says True with Newsflasharr absent, disabled, or its
    collector dead -- the event then rots in a directory nobody reads. This is
    the one thing between spooling and the inbox the operator can act on, and
    `notifier_alive` already existed in the vendored client, uncalled.
    """
    try:
        return bool(_notify_client().notifier_alive())
    except Exception:
        return False


def _schedule_health():
    """Facts about dustarr's OWN Beat row plus the last real scheduled run.

    `total_run_count` is deliberately NOT consulted: Beat counts messages SENT,
    not executed, so it reads healthy for a task the worker rejects -- which is
    precisely the outage this function exists to surface.
    """
    out = {"exists": False, "enabled": False, "queue": None,
           "last_run_ts": _read_scheduled_run_ts(), "problems": []}
    try:
        row = _periodic_task_qs().filter(name=TASK_NAME).first()
    except Exception:
        out["problems"].append("could not read the report schedule")
        return out
    if row is None:
        out["problems"].append("no report schedule is registered")
        return out
    out.update(exists=True, enabled=bool(row.enabled), queue=row.queue)
    if not row.enabled:
        out["problems"].append("the report schedule is disabled")
    if row.queue != SCHEDULE_QUEUE:
        out["problems"].append(
            f"schedule queue is {row.queue!r}, not {SCHEDULE_QUEUE!r} -- the "
            f"scheduled report will be rejected and never run")
    if out["last_run_ts"] is None:
        out["problems"].append("the scheduled report has never run")
    return out


def _create_or_update_periodic_task(*args, **kwargs):
    """Seam over core.scheduling -- Django must not be imported at module scope
    (it breaks the loader), and tests need to drive this without a DB."""
    from core.scheduling import create_or_update_periodic_task
    return create_or_update_periodic_task(*args, **kwargs)


def _delete_periodic_task(*args, **kwargs):
    from core.scheduling import delete_periodic_task
    return delete_periodic_task(*args, **kwargs)


def _periodic_task_qs():
    """The Beat row manager for dustarr's OWN schedule row."""
    from django_celery_beat.models import PeriodicTask
    return PeriodicTask.objects


def sync_schedule(settings):
    """Register (or remove) the Beat entry. Beat owns the clock, the timezone, and
    catch-up -- there is no hand-rolled scheduler (fact-check #11).

    THE QUEUE IS LOAD-BEARING AND IS RE-ASSERTED HERE (2026-07-25 outage).
    Plugin `@shared_task`s never register on Dispatcharr's PREFORK `celery`
    worker -- only on the `--pool=threads` `dvr` worker (bug-075), confirmed by
    `celery -A dispatcharr inspect registered`. `create_or_update_periodic_task`
    has NO `queue` parameter, and `run()` calls this function on EVERY action
    click, so a hand-applied `queue='dvr'` was silently destroyed by the next
    Validate/Build/Summary press. The row was found live at `queue=None`,
    `total_run_count=0`, `last_run_at=None` -- the scheduled report had never
    run at all, and nothing anywhere said so.
    """
    schedule = (coerce_settings(settings).get("report_schedule") or "weekly")
    cron = CRON_BY_SCHEDULE.get(schedule)
    if not cron:
        _delete_periodic_task(TASK_NAME)
        return
    _create_or_update_periodic_task(
        TASK_NAME,
        TASK_PATH,
        cron_expression=cron,
        enabled=True)
    # Writes dustarr's OWN schedule row, never Dispatcharr content. Bound to
    # a name so the AST guard can key its narrow allowance on the receiver --
    # see SAFE_UPDATE_RECEIVERS in tests/test_no_mutations.py.
    _schedule_row = _periodic_task_qs().filter(name=TASK_NAME)
    _schedule_row.update(queue=SCHEDULE_QUEUE)


class Plugin:
    name = "Dustarr"
    version = PLUGIN_VERSION
    description = "Channel usage metrics: which channels are watched, which never are."
    fields = FIELDS
    actions = ACTIONS

    def __init__(self):
        """C1: Plugin.run() used to be the ONLY thing that called
        ensure_collector() -- but Dispatcharr's settings-save flow never
        calls run(), and the plugin declares no `events`, so nothing else
        ever did either. A user who installs, enables, and configures the
        plugin then walks away collected NOTHING, forever -- and after
        following the README's own "restart the container after upgrading"
        advice, collection stopped dead until a button was next clicked.

        The platform hook is __init__ itself: apps/plugins/apps.py's
        ready() runs discover_plugins() in EVERY uWSGI worker (lazy-apps,
        each starts cold), and loader.py instantiates `plugin_cls()` --
        exactly this constructor. ensure_collector()'s own uWSGI-worker gate
        (a single procfs read) makes this a no-op everywhere else (Celery
        workers, manage.py shell, tests).

        Deliberately does NOT read settings here -- app-ready is explicitly
        a no-DB path (no ORM, no Redis round-trip IN THE CONSTRUCTOR itself;
        spawning the daemon thread is the one exception, and is O(ms)). The
        collector starts on DEFAULTS and the first action/settings dispatch
        refreshes it via ensure_collector's respawn-on-fingerprint-change
        path (I1). Wrapped in a bare try/except: a collector failure here
        must never break plugin construction, i.e. break Dispatcharr itself.
        """
        try:
            ensure_collector()
        except Exception:
            pass

    def stop(self, context=None):
        """Called by the loader on disable/uninstall/reload (C2). Must never
        raise -- a cleanup failure must not break the loader.

        Cleans up the two things a fresh plugin.py has no other hook for:
        (a) the orphaned Beat PeriodicTask row (every sibling plugin in this
        workspace ships an orphan-task cleaner for exactly this reason), and
        (b) the collector daemon thread, which would otherwise keep polling
        Redis and writing usage.json after the plugin is disabled.

        THE SCHEDULE ROW SURVIVES A RELOAD, AND THE `context` PARAMETER IS WHAT
        MAKES THAT POSSIBLE (measured outage 2026-08-12). Dispatcharr calls
        `stop_method(context)` and falls back to a bare `stop_method()` only
        after catching TypeError, so the no-argument signature this used to
        have discarded `context["reason"]` and deleted the row on EVERY call.
        One of those callers is the Plugins page Reload control
        (`stop_all_plugins(reason="reload")`), which is the control the deploy
        runbook says to press. Nothing re-arms the schedule at load time --
        only an action click reaches sync_schedule -- so a reload cancelled the
        weekly report outright. Two consecutive Monday reports never ran, and
        the plugin card, the version string and the collector all read healthy
        throughout.

        An absent or unrecognized reason KEEPS the row. The two failures are
        not symmetrical: an orphan row is loud, because the worker rejects a
        task it cannot import and logs it every Monday, while a missing row
        produces no report and no signal at all.
        """
        reason = context.get("reason") if isinstance(context, dict) else None
        if reason in ("disable", "delete"):
            try:
                from core.scheduling import delete_periodic_task
                delete_periodic_task(TASK_NAME)
            except Exception:
                pass

        try:
            for thread in threading.enumerate():
                if thread.name == THREAD_NAME:
                    getattr(thread, "dustarr_stop", threading.Event()).set()
        except Exception:
            pass

    def run(self, action, params=None, context=None):
        settings = (context or {}).get("settings") or {}
        try:
            ensure_collector(settings)
        except Exception:
            pass                    # a collector failure must never break an action

        try:
            sync_schedule(settings)
        except Exception:
            pass                    # I3: a scheduling failure must never break an action

        try:
            if action == "build_report":
                return self._build_and_email(settings)
            if action == "show_summary":
                return self._show_summary(settings)
            if action == "validate_settings":
                return self._validate(settings)
            if action == "report_issue":
                return self._report_issue()
        except Exception as exc:
            # Dispatcharr renders `error` (red, persistent) and `message` (a
            # transient GREEN toast); `status` renders NOWHERE. Without `error`
            # a crash is pixel-identical to success.
            redacted = redaction.redact(f"{exc}")
            return {"status": "error", "error": redacted, "message": redacted}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def _report_issue(self):
        """Surface the issue tracker's address.

        A plugin action returns data; it cannot navigate the operator's
        browser, so this prints the link rather than opening it. The URL goes
        in `file` as well as `message` because `message` is a transient toast
        that clips around 280 characters, while `file` is persistent, and a
        link you cannot select is no link at all.
        """
        return {"status": "ok",
                "message": f"Report a bug or request a feature: {ISSUES_URL}",
                "file": ISSUES_URL}

    def _build_and_email(self, settings):
        """The scheduled job, on demand: build -> verify it published -> emit.

        This was two buttons, `Build report` and `Email report now`, whose only
        difference was whether the emit step ran. Two buttons for one job made
        the operator decide something the settings already answer: the
        `notify_enabled` toggle says whether this box emails its reports, so
        the button does not need to ask again.

        Deliberately the SAME three steps as build_report_task, so a manual run
        is a REAL report rather than a re-send of an old file. It does NOT write
        `last_scheduled_run_ts` -- only the scheduled task may, or this button
        would mask a dead scheduler exactly as Newsflasharr's provenance-blind
        `last_attachment_delivered_ts` already does.

        WHAT CHANGED WITH THE MERGE, and it is a real behaviour change: the old
        email button ran its readiness PREFLIGHT before building and refused
        outright, because building a report nobody could receive was wasted
        work. Here the report is the point and is never wasted, so the report
        is ALWAYS written first and the readiness problems are reported
        afterwards, next to a `file` the operator can still open. Notifications
        being OFF is therefore no longer an error at all -- the operator did
        not ask for mail -- where the old email button had to call it one.

        The result rows are evaluated IN ORDER and are not mutually exclusive:
        `report_emitted=True` with `error` set is reachable (the report emit
        succeeds, then the gate/state block raises), so `error` outranks it.
        """
        result, model, written = _build_report(settings)
        html_path = written.get("html_path")

        # bug-078: never notify about a report that does not exist. `result`
        # already carries the red `error` key for this case, so return it whole
        # rather than rebuilding the wording here.
        if not html_path:
            return result

        # Notifications off is the normal state for an operator who just wants
        # the file. Say what happened and stop; nothing is wrong.
        if not coerce_settings(settings).get("notify_enabled"):
            return {"status": "ok",
                    "message": (result["message"] + " Notifications are off, "
                                "so nothing was emailed."),
                    "file": html_path}

        # Notifications ARE on, so the operator expects mail. Everything below
        # is a way for that to fail silently, which is why each one is `error`
        # (the only persistent red surface) rather than just `status`.
        blockers = _newsflasharr_readiness()
        if not blockers and not _notifier_alive():
            blockers.append("Newsflasharr's collector has not ticked recently, "
                            "so a spooled event would sit unsent.")
        if blockers:
            msg = ("Report built, but it cannot be emailed. "
                   + " ".join(blockers))
            return {"status": "error", "message": msg, "error": msg,
                    "file": html_path}

        emit = _emit_notifications(settings, model, written) or {}

        if emit.get("error"):
            msg = f"Report built, but the notification failed: {emit['error']}"
            return {"status": "error", "message": msg, "error": msg,
                    "file": html_path}
        if not emit.get("report_emitted"):
            msg = "Report built, but Newsflasharr did not accept the event."
            return {"status": "error", "message": msg, "error": msg,
                    "file": html_path}
        if not _notifier_alive():
            msg = ("Report spooled, but Newsflasharr's collector has not ticked "
                   "recently, so nothing will send it.")
            return {"status": "error", "message": msg, "error": msg,
                    "file": html_path}

        # QUEUED, never "sent": notify() returning True means durably spooled;
        # Newsflasharr's collector delivers later on its own retry ladder, and
        # an SMTP 250 is acceptance for relay, not delivery.
        return {"status": "ok",
                "message": ("Report built and queued for delivery to "
                            "Newsflasharr. The honesty-gate check ran too, as "
                            "it does on the schedule."),
                "file": html_path}

    def _show_summary(self, settings):
        thresholds = coerce_settings(settings)
        store = storage.Storage(DATA_DIR)
        now = _gateway().now()      # M8: same clock source as _build_report
        usage = store.load(now)
        channels = (usage.get("channels") or {})
        meta = usage.get("meta") or {}
        stats_since = meta.get("stats_since")
        if not stats_since:
            return {"status": "ok",
                    "message": "Collecting. No usage data recorded yet."}

        days = (now - float(stats_since)) / 86400.0
        coverage = gates.coverage_fraction(
            meta.get("coverage"), now,
            window_days=max(min(days, thresholds["unused_threshold_days"]), 1.0),
            poll_interval_s=thresholds["poll_interval_s"],
            client_gap_grace_s=thresholds["client_gap_grace_s"])
        watched = sum(1 for r in channels.values() if r.get("watch_count"))
        return {"status": "ok",
                "message": (f"Tracking {days:.1f} days · coverage {coverage:.0%} · "
                            f"{watched} channels watched. Build the report for the "
                            f"never-watched list.")}

    def _validate(self, settings):
        thresholds = coerce_settings(settings)
        raw_re = settings.get("exclude_name_regex",
                              gateway.DEFAULT_EXCLUDE_NAME_RE)
        if raw_re:
            import re
            try:
                re.compile(str(raw_re))
            except re.error as exc:
                # `error` as well as `status`: the plugin card renders `.error`
                # (red, persistent) and `message` (a transient GREEN toast), but
                # `status` NOWHERE -- so a failure without `error` looks exactly
                # like a success.
                msg = redaction.redact(
                    f"Excluded name regex does not compile: {exc}")
                return {"status": "error", "message": msg, "error": msg}

        # sync_schedule already ran once in run() before dispatch (I3) -- that
        # is the single arming site. Calling it again here would just arm the
        # schedule twice per click for no benefit.
        health = _schedule_health()
        problems = list(health["problems"])
        # Only meaningful when notifications are ON -- flagging a dead collector
        # for an operator who is not using it is a false alarm, which is its own
        # defect.
        if thresholds.get("notify_enabled") and not _notifier_alive():
            problems.append(
                "Newsflasharr's collector has not ticked recently -- report "
                "emails will be spooled but never sent")
        if problems:
            return {"status": "error", "message": problems[0],
                    "error": "; ".join(problems)}

        if health["last_run_ts"] is not None:
            age_d = (time.time() - health["last_run_ts"]) / 86400.0
            ran = f" Scheduled report last ran {age_d:.1f} days ago."
        else:
            ran = ""
        return {"status": "ok",
                "message": (f"Settings OK. Poll {thresholds['poll_interval_s']}s, "
                            f"min watch {thresholds['min_watch_seconds']:.0f}s, "
                            f"report {thresholds['report_schedule']}.{ran}")}
