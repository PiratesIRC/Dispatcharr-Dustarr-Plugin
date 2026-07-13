"""Metricsarr webhook — generic JSON or Discord envelope. Stdlib only.

Provider credentials live INSIDE stream URLs in this deployment, so every string
that reaches a payload passes through redact(). The payload is built from an
allowlist -- Stream.url and M3UAccount are unreachable from here by construction
(spec S7.1).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT_PREFIX = "Dispatcharr-Metricsarr/"
DISCORD_LIMIT = 2000
TIMEOUT_S = 10

# http://host/live|movie|series/<user>/<pass>/rest -> creds replaced
_CREDS_RE = re.compile(r"(/(?:live|movie|series)/)[^/\s]+/[^/\s]+/",
                       re.IGNORECASE)


def redact(text):
    if text is None:
        return None
    return _CREDS_RE.sub(r"\1<redacted>/<redacted>/", str(text))


def _clean(value):
    """Recursively redact every string that can reach the wire."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def build_payload(summary, fmt, version):
    allow = ("tracked_days", "coverage", "total_channels", "never_watched",
             "tuned_never_qualified", "top", "report_url", "alerts")
    data = {k: _clean(summary.get(k)) for k in allow if k in summary}

    if fmt == "discord":
        lines = [
            "**Metricsarr — usage report**",
            f"Tracking {data.get('tracked_days', 0)} days · "
            f"coverage {float(data.get('coverage') or 0):.0%}",
            f"**{data.get('never_watched', 0)}** of {data.get('total_channels', 0)} "
            f"channels never watched",
        ]
        if data.get("tuned_never_qualified"):
            lines.append(f"{data['tuned_never_qualified']} tuned but never "
                         f"qualified — likely broken, check the report")
        for alert in (data.get("alerts") or []):
            lines.append(f"⚠️ {alert}")
        if data.get("report_url"):
            lines.append(f"Full report: {data['report_url']}")

        content = "\n".join(lines)
        if len(content) > DISCORD_LIMIT:
            content = content[:DISCORD_LIMIT - 3] + "..."
        return json.dumps({"content": content}).encode("utf-8")

    body = {"plugin": "metricsarr", "event": "usage_report", "version": version}
    body.update(data)
    return json.dumps(body).encode("utf-8")


def fire(url, summary, fmt, version, timeout=TIMEOUT_S, opener=None):
    url = (url or "").strip()
    if not url:
        return {"status": "error", "message": "No webhook URL configured."}
    if not url.startswith(("http://", "https://")):
        return {"status": "error",
                "message": "Webhook URL must start with http:// or https://"}

    payload = build_payload(summary, fmt, version)
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 # Discord's Cloudflare edge 403s the default Python-urllib UA
                 # and silently drops every webhook.
                 "User-Agent": f"{USER_AGENT_PREFIX}{version}"})

    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            status = getattr(response, "status", 0)
        return {"status": "ok", "message": f"Webhook sent (HTTP {status})."}
    except urllib.error.HTTPError as exc:
        return {"status": "error",
                "message": redact(f"Webhook failed: HTTP {exc.code}")}
    except Exception as exc:                       # never raises to the caller
        return {"status": "error", "message": redact(f"Webhook failed: {exc}")}
