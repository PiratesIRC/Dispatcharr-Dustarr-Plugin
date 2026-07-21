import pytest
from conftest import load_pure


@pytest.fixture()
def rd():
    return load_pure("redaction")


def test_redact_strips_provider_credentials(rd):
    dirty = ("failed on http://edge-23.provider.tv/live/joe123/s3cr3tpass/100001.ts "
             "after 3 tries")
    clean = rd.redact(dirty)
    assert "s3cr3tpass" not in clean
    assert "joe123" not in clean
    assert "redacted" in clean


def test_redact_handles_movie_and_series_paths(rd):
    for kind in ("live", "movie", "series"):
        dirty = f"http://h.tv/{kind}/user/pass/1.ts"
        assert "pass" not in rd.redact(dirty)


def test_redact_strips_xtream_query_string_credentials_get_php(rd):
    dirty = ("Failed refreshing http://edge-23.provider.tv/get.php"
              "?username=joe123&password=s3cr3tpass&type=m3u_plus")
    clean = rd.redact(dirty)
    assert "joe123" not in clean
    assert "s3cr3tpass" not in clean
    assert "redacted" in clean


def test_redact_strips_xtream_query_string_credentials_player_api(rd):
    dirty = ("player_api.php request to http://h.tv/player_api.php"
              "?username=joe123&password=s3cr3tpass failed: 403")
    clean = rd.redact(dirty)
    assert "joe123" not in clean
    assert "s3cr3tpass" not in clean


def test_redact_handles_password_segment_at_end_of_string(rd):
    clean = rd.redact("http://h.tv/live/user/pass")
    assert "pass" not in clean
    assert "user" not in clean
    assert "redacted" in clean


def test_redact_handles_password_segment_before_query_string(rd):
    clean = rd.redact("http://h.tv/live/joe/pw1?p=1")
    assert "pw1" not in clean
    assert "joe" not in clean


def test_redact_handles_basic_auth_in_host(rd):
    clean = rd.redact("http://user:pass@host/live/feed.ts")
    assert "user" not in clean
    assert "pass" not in clean


def test_redact_returns_none_for_none(rd):
    assert rd.redact(None) is None
