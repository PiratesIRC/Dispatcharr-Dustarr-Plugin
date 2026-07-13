import json

import pytest
from conftest import load_pure


@pytest.fixture()
def wh():
    return load_pure("webhook")


SUMMARY = {
    "tracked_days": 31,
    "coverage": 0.992,
    "total_channels": 1440,
    "never_watched": 1187,
    "tuned_never_qualified": 6,
    "top": [{"name": "CNN", "watch_count": 22, "hours": 14.5}],
    "report_url": "http://192.168.1.53:9191/logos/metricsarr/report.html",
    "alerts": [],
}


def test_redact_strips_provider_credentials(wh):
    dirty = ("failed on http://edge-23.provider.tv/live/joe123/s3cr3tpass/100001.ts "
             "after 3 tries")
    clean = wh.redact(dirty)
    assert "s3cr3tpass" not in clean
    assert "joe123" not in clean
    assert "redacted" in clean


def test_redact_handles_movie_and_series_paths(wh):
    for kind in ("live", "movie", "series"):
        dirty = f"http://h.tv/{kind}/user/pass/1.ts"
        assert "pass" not in wh.redact(dirty)


def test_generic_payload_is_machine_readable(wh):
    body = json.loads(wh.build_payload(SUMMARY, "generic", "1.26.0"))
    assert body["plugin"] == "metricsarr"
    assert body["never_watched"] == 1187
    assert body["report_url"].endswith("report.html")


def test_discord_payload_uses_a_content_envelope(wh):
    body = json.loads(wh.build_payload(SUMMARY, "discord", "1.26.0"))
    assert set(body) == {"content"}
    assert "1187" in body["content"]
    assert "report.html" in body["content"]


def test_discord_content_stays_under_the_2000_char_limit(wh):
    big = dict(SUMMARY)
    big["top"] = [{"name": "X" * 80, "watch_count": i, "hours": 1.0}
                  for i in range(200)]
    body = json.loads(wh.build_payload(big, "discord", "1.26.0"))
    assert len(body["content"]) <= 2000


def test_payload_cannot_carry_a_stream_url_even_if_one_leaks_into_alerts(wh):
    """Credential safety is structural: everything serialized goes through the
    redactor, so no code path can publish a provider URL (spec S7.1)."""
    leaky = dict(SUMMARY)
    leaky["alerts"] = ["boom: http://edge-23.provider.tv/live/joe/pw123/1.ts"]
    for fmt in ("generic", "discord"):
        raw = wh.build_payload(leaky, fmt, "1.26.0").decode()
        assert "pw123" not in raw
        assert "joe" not in raw


def test_fire_sets_an_explicit_user_agent(wh):
    sent = {}

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(req, timeout=None):
        sent["ua"] = req.get_header("User-agent")
        sent["url"] = req.full_url
        sent["timeout"] = timeout
        return FakeResponse()

    result = wh.fire("https://discord.com/api/webhooks/x", SUMMARY, "discord",
                     "1.26.0", opener=opener)
    # Discord's Cloudflare edge 403s Python-urllib's default UA and silently
    # drops every webhook (iptv-checker learned this the hard way).
    assert sent["ua"].startswith(wh.USER_AGENT_PREFIX)
    assert sent["timeout"] == 10
    assert result["status"] == "ok"


def test_fire_never_raises_on_a_dead_endpoint(wh):
    def opener(req, timeout=None):
        raise OSError("connection refused")

    result = wh.fire("https://example.invalid/hook", SUMMARY, "generic", "1.26.0",
                     opener=opener)
    assert result["status"] == "error"
    assert "connection refused" in result["message"]


def test_fire_rejects_a_missing_or_malformed_url(wh):
    assert wh.fire("", SUMMARY, "generic", "1.0")["status"] == "error"
    assert wh.fire("ftp://x/y", SUMMARY, "generic", "1.0")["status"] == "error"


def test_fire_redacts_credentials_out_of_its_own_error_message(wh):
    def opener(req, timeout=None):
        raise OSError("failed posting http://h.tv/live/user/hunter2/1.ts")

    result = wh.fire("https://example.com/hook", SUMMARY, "generic", "1.0",
                     opener=opener)
    assert "hunter2" not in result["message"]
