"""Metricsarr webhook — generic JSON or Discord envelope. Stdlib only.

Provider credentials live INSIDE stream URLs in this deployment, so every string
that reaches a payload passes through redact(). The payload is built from an
allowlist -- Stream.url and M3UAccount are unreachable from here by construction
(spec S7.1).

The redact() function is a shape-matcher, not a semantic scrubber. It covers the
credential transports this deployment actually uses: path creds (/live|movie|series/<user>/<pass>/),
Xtream query creds (?username=&password=), and basic-auth-in-host. It does NOT cover
other transports (?token=, ?pwd=, ?auth=, ?key=, ?secret=, Authorization header, etc.)
because those are unreachable today (alerts are produced solely by gates.py with
hand-authored literals containing no URLs). The allowlist is the load-bearing control;
the redactor is defense-in-depth. If a future caller ever puts exception text or provider
URLs into alerts, the right fix is to redact at the PRODUCER, not to widen these regexes
to guess at every possible transport.
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

# http://host/live|movie|series/<user>/<pass>/rest -> creds replaced.
# The password segment may be terminated by '/', '?', whitespace, or the end
# of the string -- not just '/' -- so a truncated log line or a bare
# playlist URL (no trailing path/query) still gets redacted.
_CREDS_RE = re.compile(r"(/(?:live|movie|series)/)[^/\s?]+/[^/\s?]+(?=[/?\s]|$)",
                       re.IGNORECASE)

# Xtream Codes query-string auth: get.php / player_api.php etc take
# username=...&password=... (also user=/pass=) as plain query params.
_QUERY_CREDS_RE = re.compile(r"([?&](?:username|user|password|pass)=)[^&\s]+",
                             re.IGNORECASE)

# http://user:pass@host/... basic-auth-in-host form.
_BASIC_AUTH_RE = re.compile(r"(://)[^/\s@]+:[^/\s@]+(@)")


def redact(text):
    if text is None:
        return None
    out = str(text)
    out = _CREDS_RE.sub(r"\1<redacted>/<redacted>", out)
    out = _QUERY_CREDS_RE.sub(r"\1<redacted>", out)
    out = _BASIC_AUTH_RE.sub(r"\1<redacted>:<redacted>\2", out)
    return out


def _clean(value):
    """Recursively redact every string that can reach the wire."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: _clean(v) for k, v in value.items()}
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
        return json.dumps({"content": content,
                          "allowed_mentions": {"parse": []}}).encode("utf-8")

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

    try:
        payload = build_payload(summary, fmt, version)
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     # Discord's Cloudflare edge 403s the default Python-urllib
                     # UA and silently drops every webhook.
                     "User-Agent": f"{USER_AGENT_PREFIX}{version}"})
    except Exception as exc:                        # never raises to the caller
        return {"status": "error", "message": redact(f"Webhook failed: {exc}")}

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
