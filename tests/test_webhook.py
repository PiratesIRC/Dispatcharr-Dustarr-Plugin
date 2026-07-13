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


def test_redact_strips_xtream_query_string_credentials_get_php(wh):
    dirty = ("Failed refreshing http://edge-23.provider.tv/get.php"
              "?username=joe123&password=s3cr3tpass&type=m3u_plus")
    clean = wh.redact(dirty)
    assert "joe123" not in clean
    assert "s3cr3tpass" not in clean
    assert "redacted" in clean


def test_redact_strips_xtream_query_string_credentials_player_api(wh):
    dirty = ("player_api.php request to http://h.tv/player_api.php"
              "?username=joe123&password=s3cr3tpass failed: 403")
    clean = wh.redact(dirty)
    assert "joe123" not in clean
    assert "s3cr3tpass" not in clean


def test_redact_handles_password_segment_at_end_of_string(wh):
    clean = wh.redact("http://h.tv/live/user/pass")
    assert "pass" not in clean
    assert "user" not in clean
    assert "redacted" in clean


def test_redact_handles_password_segment_before_query_string(wh):
    clean = wh.redact("http://h.tv/live/joe/pw1?p=1")
    assert "pw1" not in clean
    assert "joe" not in clean


def test_redact_handles_basic_auth_in_host(wh):
    clean = wh.redact("http://user:pass@host/live/feed.ts")
    assert "user" not in clean
    assert "pass" not in clean


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
    # `top` is never rendered in the Discord branch -- growing it exercises
    # nothing. `alerts` IS rendered (one line per entry), so that's what has
    # to grow to genuinely hit the truncation path.
    big = dict(SUMMARY)
    big["alerts"] = [f"channel {i} tuned but never qualified, host edge-{i}"
                      for i in range(200)]
    raw = wh.build_payload(big, "discord", "1.26.0")
    body = json.loads(raw)
    assert len(body["content"]) <= 2000
    assert json.loads(raw.decode())["content"] == body["content"]


def test_payload_cannot_carry_a_stream_url_even_if_one_leaks_into_alerts(wh):
    """Credential safety is structural: everything serialized goes through the
    redactor, so no code path can publish a provider URL (spec S7.1)."""
    leaky = dict(SUMMARY)
    leaky["alerts"] = ["boom: http://edge-23.provider.tv/live/joe/pw123/1.ts"]
    for fmt in ("generic", "discord"):
        raw = wh.build_payload(leaky, fmt, "1.26.0").decode()
        assert "pw123" not in raw
        assert "joe" not in raw


def test_build_payload_only_serializes_allowlisted_keys(wh):
    """Mutation test for the allowlist: an unlisted key holding a raw secret
    must never reach either payload format, even nested."""
    leaky = dict(SUMMARY)
    leaky["debug_context"] = {"m3u_password": "hunter2"}
    for fmt in ("generic", "discord"):
        raw = wh.build_payload(leaky, fmt, "1.26.0").decode()
        assert "hunter2" not in raw
        assert "debug_context" not in raw
        assert "m3u_password" not in raw


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


def test_fire_never_raises_on_a_summary_that_is_none(wh):
    """An upstream caller bug (summary=None) must not crash fire()."""
    result = wh.fire("https://example.com/hook", None, "generic", "1.0",
                     opener=lambda req, timeout=None: (_ for _ in ()).throw(
                         AssertionError("should not reach the network")))
    assert result["status"] == "error"


def test_fire_never_raises_when_summary_contains_a_non_json_serializable_set(wh):
    bad = dict(SUMMARY)
    bad["alerts"] = {"http://h.tv/live/joe/pw1/1.ts"}
    result = wh.fire("https://example.com/hook", bad, "generic", "1.0",
                     opener=lambda req, timeout=None: (_ for _ in ()).throw(
                         AssertionError("should not reach the network")))
    assert result["status"] == "error"


def test_fire_never_raises_when_summary_contains_bytes(wh):
    bad = dict(SUMMARY)
    bad["alerts"] = [b"not a string"]
    result = wh.fire("https://example.com/hook", bad, "generic", "1.0",
                     opener=lambda req, timeout=None: (_ for _ in ()).throw(
                         AssertionError("should not reach the network")))
    assert result["status"] == "error"
