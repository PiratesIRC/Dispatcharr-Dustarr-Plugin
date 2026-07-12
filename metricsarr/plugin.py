"""Metricsarr — channel usage metrics for Dispatcharr. Read-only.

Phase 1 mutates NOTHING: it polls Redis, writes its own files, and reports.
Spec: docs/superpowers/specs/2026-07-12-metricsarr-design.md (rev 2).

Loader contract shell. Plugin.__init__ is O(ms) and I/O-free.
"""
from __future__ import annotations

PLUGIN_VERSION = "1.26.1931200"

DATA_DIR = "/data/metricsarr"          # plugin state (named volume)
REPORT_DIR = "/data/logos/metricsarr"  # nginx serves /data/logos/** at /logos/**
CSV_DIR = "/config/metricsarr"         # bind mount -> <config-mount>

FIELDS = [
    {"id": "poll_interval_s", "label": "Poll interval (s)", "type": "number",
     "default": 15,
     "description": "Sampling cadence. Must stay well under Dispatcharr's 30s "
                    "metadata TTL or live channels are missed between refreshes."},
    {"id": "min_watch_seconds", "label": "Minimum watch (s)", "type": "number",
     "default": 120,
     "description": "A session shorter than this is a channel-surf, not a watch. "
                    "It still updates 'last tuned'."},
]

ACTIONS = [
    {"id": "show_summary", "label": "Show summary",
     "description": "Tracking window, coverage, and never-watched count.",
     "button_label": "Summary"},
]


class Plugin:
    name = "Metricsarr"
    version = PLUGIN_VERSION
    description = "Channel usage metrics: which channels are watched, which never are."
    fields = FIELDS
    actions = ACTIONS

    def run(self, action, params=None, context=None):
        settings = (context or {}).get("settings") or {}
        if action == "show_summary":
            return self._show_summary(settings)
        return {"status": "error", "message": f"Unknown action: {action}"}

    def _show_summary(self, settings):
        return {"status": "ok", "message": "Metricsarr: collector not yet started."}
