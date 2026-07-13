"""Metricsarr — channel usage metrics for Dispatcharr. READ-ONLY.

Phase 1 mutates NOTHING in Dispatcharr: it polls Redis, writes its own files, and
reports. Spec: docs/superpowers/specs/2026-07-12-metricsarr-design.md (rev 2).

Plugin.__init__ is O(ms) and I/O-free (the procfs read is exempt). Django imports
are function-local -- a module-level Django import breaks the loader.
"""
from __future__ import annotations

import os
import threading
import time

try:
    from . import collector as collector_mod
    from . import gates, gateway, reports, sessionizer, storage, webhook
except ImportError:                     # standalone (non-package) import path
    import collector as collector_mod
    import gates
    import gateway
    import reports
    import sessionizer
    import storage
    import webhook

PLUGIN_VERSION = "1.26.1931200"

DATA_DIR = "/data/metricsarr"           # plugin state (named volume)
REPORT_DIR = "/data/logos/metricsarr"   # nginx serves /data/logos/** at /logos/**
CSV_DIR = "/config/metricsarr"          # bind mount -> <config-mount>

THREAD_NAME = "metricsarr-collector"
TASK_NAME = "metricsarr_build_report"
RESTART_BOUND = 5
RESTART_WINDOW_S = 3600.0

# The metadata key's TTL is 30s (refreshed every 1s). A poll slower than this can
# miss a live channel between refreshes entirely (fact-check #3).
MAX_POLL_INTERVAL_S = 25

_restart_times = []
_spawn_lock = threading.Lock()

FIELDS = [
    {"id": "info", "type": "info", "label": "Metricsarr is read-only",
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
    {"id": "top_n", "label": "Top/bottom N", "type": "number", "default": 20},
    {"id": "never_watched_ceiling", "label": "Never-watched alarm ceiling",
     "type": "number", "default": 0.6,
     "description": "If more than this fraction of channels look never-watched, "
                    "the data is treated as untrustworthy (a blind collector)."},
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
    {"id": "webhook_url", "label": "Webhook URL", "type": "string", "default": "",
     "input_type": "password",
     "description": "Discord or any JSON endpoint. Gets a short nudge with a link "
                    "to the full report."},
    {"id": "webhook_format", "label": "Webhook format", "type": "select",
     "default": "discord",
     "options": [{"value": "discord", "label": "Discord"},
                 {"value": "generic", "label": "Generic JSON"}]},
    {"id": "report_schedule", "label": "Scheduled report", "type": "select",
     "default": "weekly",
     "options": [{"value": "off", "label": "Off"},
                 {"value": "daily", "label": "Daily (03:00)"},
                 {"value": "weekly", "label": "Weekly (Mon 03:00)"},
                 {"value": "monthly", "label": "Monthly (1st, 03:00)"}]},
]

ACTIONS = [
    {"id": "build_report", "label": "Build report",
     "description": "Write the HTML report and CSV now.",
     "button_label": "Build"},
    {"id": "show_summary", "label": "Show summary",
     "description": "Tracking window, coverage, never-watched count.",
     "button_label": "Summary"},
    {"id": "send_webhook_now", "label": "Send webhook",
     "description": "Fire the webhook nudge immediately (tests your URL).",
     "button_label": "Send"},
    {"id": "validate_settings", "label": "Validate settings",
     "description": "Check every setting parses.", "button_label": "Validate"},
]

_NUMERIC_FLOORS = {"poll_interval_s": (5, MAX_POLL_INTERVAL_S),
                   "min_watch_seconds": (10, 3600),
                   "client_gap_grace_s": (30, 600),
                   "merge_gap_s": (0, 600),
                   "unused_threshold_days": (1, 3650),
                   "top_n": (1, 500),
                   "never_watched_ceiling": (0.05, 1.0)}


def coerce_settings(settings):
    """Settings arrive UNVALIDATED from the API: coerce and floor everything."""
    settings = settings or {}
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
            low, high = _NUMERIC_FLOORS.get(fid, (None, None))
            if low is not None:
                value = max(low, min(high, value))
            if fid in ("top_n", "unused_threshold_days"):
                value = int(value)
            elif fid == "poll_interval_s" and value == int(value):
                value = int(value)
        elif field["type"] == "boolean":
            value = bool(value)
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
            if stop_event.wait(wait):
                break
        if col is not None:
            col.shutdown()

    thread = threading.Thread(target=loop, name=THREAD_NAME, daemon=True)
    thread.metricsarr_version = PLUGIN_VERSION
    thread.metricsarr_stop = stop_event
    thread.start()
    return thread


def ensure_collector(settings=None):
    """Idempotent: one live collector thread per worker, superseded on version bump."""
    if not _is_uwsgi_worker():
        return
    with _spawn_lock:
        live = [t for t in threading.enumerate()
                if t.name == THREAD_NAME and t.is_alive()]
        for thread in live:
            if getattr(thread, "metricsarr_version", None) == PLUGIN_VERSION:
                return
            getattr(thread, "metricsarr_stop", threading.Event()).set()

        now = time.time()
        _restart_times[:] = [t for t in _restart_times if now - t < RESTART_WINDOW_S]
        if len(_restart_times) >= RESTART_BOUND:
            return                          # crash-loop bound
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

    url = written.get("url")
    result = {"status": "ok", "message": message + f" Report: {url}",
              "file": written.get("html_path") or url}
    if written.get("error"):
        result["message"] += f" ({written['error']})"
    return result, model, written


def _send_webhook(settings, model, written):
    thresholds = coerce_settings(settings)
    url = (thresholds.get("webhook_url") or "").strip()
    if not url:
        return {"status": "error",
                "message": "No webhook URL configured in plugin settings."}
    summary = reports.summary_for_webhook(model, reports.REPORT_URL_PATH)
    return webhook.fire(url, summary, thresholds.get("webhook_format", "discord"),
                        PLUGIN_VERSION)


def build_report_task():
    """Celery entry point. Runs on the PREFORK queue -- real processes, no gevent,
    so ORM-heavy work here cannot wedge a uWSGI worker (bug-117)."""
    from django.db import close_old_connections

    close_old_connections()
    settings = _load_settings()
    _, model, _ = _build_report(settings)
    if (settings.get("webhook_url") or "").strip():
        _send_webhook(settings, model, None)
    return model["counts"]


def _load_settings():
    from apps.plugins.models import PluginConfig  # Dispatcharr runtime only

    try:
        config = PluginConfig.objects.get(key="metricsarr")
        return config.settings or {}
    except Exception:
        return {}


CRON_BY_SCHEDULE = {"daily": "0 3 * * *", "weekly": "0 3 * * 1",
                    "monthly": "0 3 1 * *"}


def sync_schedule(settings):
    """Register (or remove) the Beat entry. Beat owns the clock, the timezone, and
    catch-up -- there is no hand-rolled scheduler (fact-check #11)."""
    from core.scheduling import create_or_update_periodic_task, delete_periodic_task

    schedule = (coerce_settings(settings).get("report_schedule") or "weekly")
    cron = CRON_BY_SCHEDULE.get(schedule)
    if not cron:
        delete_periodic_task(TASK_NAME)
        return
    create_or_update_periodic_task(
        TASK_NAME,
        "_dispatcharr_plugin_metricsarr.plugin.build_report_task",
        cron_expression=cron,
        enabled=True)


class Plugin:
    name = "Metricsarr"
    version = PLUGIN_VERSION
    description = "Channel usage metrics: which channels are watched, which never are."
    fields = FIELDS
    actions = ACTIONS

    def __init__(self):
        # I/O-free: the collector is started on the first action/settings dispatch,
        # not here.
        pass

    def run(self, action, params=None, context=None):
        settings = (context or {}).get("settings") or {}
        try:
            ensure_collector(settings)
        except Exception:
            pass                    # a collector failure must never break an action

        try:
            if action == "build_report":
                result, _, _ = _build_report(settings)
                return result
            if action == "show_summary":
                return self._show_summary(settings)
            if action == "send_webhook_now":
                _, model, written = _build_report(settings)
                return _send_webhook(settings, model, written)
            if action == "validate_settings":
                return self._validate(settings)
        except Exception as exc:
            return {"status": "error", "message": webhook.redact(f"{exc}")}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def _show_summary(self, settings):
        thresholds = coerce_settings(settings)
        store = storage.Storage(DATA_DIR)
        now = time.time()
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
                return {"status": "error",
                        "message": f"Excluded name regex does not compile: {exc}"}

        url = (thresholds.get("webhook_url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            return {"status": "error",
                    "message": "Webhook URL must start with http:// or https://"}

        try:
            sync_schedule(settings)
        except Exception as exc:
            return {"status": "ok",
                    "message": f"Settings OK, but scheduling failed: {exc}"}

        return {"status": "ok",
                "message": (f"Settings OK. Poll {thresholds['poll_interval_s']}s, "
                            f"min watch {thresholds['min_watch_seconds']:.0f}s, "
                            f"report {thresholds['report_schedule']}.")}
