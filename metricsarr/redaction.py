"""Metricsarr redaction — credential-scrubbing regexes. Stdlib only.

Provider credentials live INSIDE stream URLs in this deployment, so every string
that reaches a notification (title, body, or a logged error) passes through
redact(). The report/notify payload is built from an allowlist -- Stream.url and
M3UAccount are unreachable from here by construction (spec S7.1).

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

import re

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
