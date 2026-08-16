from unittest.mock import MagicMock

import pytest
from conftest import load_plugin

load_plugin()  # installs the Django stubs before gateway imports anything


@pytest.fixture()
def gw():
    import sys
    return sys.modules["dustarr_under_test.gateway"]


def row(gw, **kw):
    base = {"id": 1, "uuid": "u1", "name": "CNN", "group": "US: News",
            "auto_created": False, "created_at": 1000.0, "proxying": True}
    base.update(kw)
    return gw.ChannelRow(**base)


def test_parse_exclusions_defaults(gw):
    ex = gw.parse_exclusions({})
    assert ex["exclude_auto_created"] is True
    assert "us: ppv" in ex["groups"]
    assert ex["name_re"].search("LIVE EVENT 04 - Sturm v Stein")


def test_parse_exclusions_tolerates_a_broken_regex(gw):
    """A bad regex must not wedge the report -- it degrades to no name rule."""
    ex = gw.parse_exclusions({"exclude_name_regex": "((("})
    assert ex["name_re"] is None


def test_parse_exclusions_splits_and_strips_groups(gw):
    ex = gw.parse_exclusions({"exclude_groups": " US: News , UK: All "})
    assert ex["groups"] == {"us: news", "uk: all"}


def test_auto_created_channel_is_excluded(gw):
    ex = gw.parse_exclusions({})
    assert gw.classify(row(gw, auto_created=True), ex, 2000.0) == "excluded:auto_created"


def test_auto_created_exclusion_can_be_turned_off(gw):
    ex = gw.parse_exclusions({"exclude_auto_created": False,
                              "exclude_groups": "", "exclude_name_regex": ""})
    assert gw.classify(row(gw, auto_created=True), ex, 2000.0) is None


def test_group_exclusion_is_case_insensitive(gw):
    ex = gw.parse_exclusions({"exclude_groups": "us: news"})
    assert gw.classify(row(gw, group="US: News"), ex, 2000.0) == "excluded:group"


def test_name_regex_exclusion(gw):
    ex = gw.parse_exclusions({"exclude_groups": "",
                              "exclude_name_regex": "(?i)LIVE EVENT"})
    assert gw.classify(row(gw, name="LIVE EVENT 12 - NO EVENT", auto_created=False),
                       ex, 2000.0) == "excluded:name"


def test_non_proxying_channel_is_unobservable_never_unused(gw):
    """A Redirect-profile channel never writes live:channel:* -- it would read as
    never-watched forever. It must be reported as unobservable, never as unused."""
    ex = gw.parse_exclusions({"exclude_groups": "", "exclude_name_regex": ""})
    assert gw.classify(row(gw, proxying=False), ex, 2000.0) == "unobservable"


def test_eligible_channel_returns_no_reason(gw):
    ex = gw.parse_exclusions({"exclude_groups": "", "exclude_name_regex": ""})
    assert gw.classify(row(gw, name="AMC", group="US: Entertainment"),
                       ex, 2000.0) is None


def test_unobservable_wins_over_nothing_but_loses_to_explicit_exclusions(gw):
    """Ordering matters for the report's `reason` column: an explicitly excluded
    channel reports its exclusion, not its observability."""
    ex = gw.parse_exclusions({})
    reason = gw.classify(row(gw, auto_created=True, proxying=False), ex, 2000.0)
    assert reason == "excluded:auto_created"


def test_default_group_exclusion_matches_real_case(gw):
    """The default exclusion set must match real-world channel group casing
    end-to-end, proving the normalization is consistent."""
    ex = gw.parse_exclusions({})
    assert gw.classify(row(gw, group="US: PPV"), ex, 2000.0) == "excluded:group"


# -- I3: _is_proxying must resolve the GLOBAL default stream profile, not
# hardcode NULL -> True. 1438 of 1440 real channels carry stream_profile=NULL,
# and Dispatcharr resolves that NULL to the global default AT PLAY TIME -- not
# to an unconditional "always proxying" answer.

def test_is_proxying_none_profile_inherits_a_proxying_default(gw):
    channel = type("C", (), {"stream_profile": None})()
    assert gw._is_proxying(channel, default_is_redirect=False) is True


def test_is_proxying_none_profile_inherits_a_redirect_default(gw):
    """If the operator ever points the global default at Redirect, every NULL
    channel must become unobservable -- not silently keep reading as proxying."""
    channel = type("C", (), {"stream_profile": None})()
    assert gw._is_proxying(channel, default_is_redirect=True) is False


def test_is_proxying_none_profile_fails_safe_when_default_is_unresolved(gw):
    """A lookup failure (default_is_redirect=None) must never manufacture a
    false "not proxying" verdict -- fail SAFE, same as the old behavior."""
    channel = type("C", (), {"stream_profile": None})()
    assert gw._is_proxying(channel, default_is_redirect=None) is True


def test_is_proxying_explicit_profile_ignores_the_resolved_default(gw):
    profile = type("P", (), {"name": "Redirect"})()
    channel = type("C", (), {"stream_profile": profile})()
    assert gw._is_proxying(channel, default_is_redirect=False) is False


def test_is_proxying_prefers_the_profile_own_structural_answer(gw):
    """Dispatcharr decides redirect-versus-proxy at play time via
    StreamProfile.is_redirect() (locked AND named exactly 'Redirect'). A
    profile whose is_redirect() answers must be believed over its name: a
    cloned 'Redirect (302)' profile is PROXIED by Dispatcharr and writes the
    Redis keys the collector reads, so it is observable."""
    clone = type("P", (), {"name": "Redirect (302)",
                           "is_redirect": lambda self: False})()
    channel = type("C", (), {"stream_profile": clone})()
    assert gw._is_proxying(channel, default_is_redirect=False) is True

    locked_redirect = type("P", (), {"name": "Redirect",
                                     "is_redirect": lambda self: True})()
    channel = type("C", (), {"stream_profile": locked_redirect})()
    assert gw._is_proxying(channel, default_is_redirect=False) is False


def test_is_proxying_name_matched_redirect_lookalike_is_not_observable_without_structure(gw):
    """Fallback only: with no is_redirect() available (an older Dispatcharr),
    the name heuristic still applies."""
    profile = type("P", (), {"name": "  DIRECT "})()
    channel = type("C", (), {"stream_profile": profile})()
    assert gw._is_proxying(channel, default_is_redirect=False) is False


def test_is_proxying_falls_back_to_the_name_when_is_redirect_raises(gw):
    def boom(self):
        raise RuntimeError("deferred field load failed")

    profile = type("P", (), {"name": "Redirect", "is_redirect": boom})()
    channel = type("C", (), {"stream_profile": profile})()
    assert gw._is_proxying(channel, default_is_redirect=False) is False


def test_default_stream_profile_name_resolves_and_lowercases(gw, monkeypatch):
    import sys

    core_models = sys.modules["core.models"]
    apps_models = sys.modules["apps.channels.models"]
    fake_profile = MagicMock()
    fake_profile.name = "Redirect"
    fake_queryset = MagicMock()
    fake_queryset.get.return_value = fake_profile
    monkeypatch.setattr(core_models.CoreSettings, "get_default_stream_profile_id",
                        MagicMock(return_value=4))
    monkeypatch.setattr(apps_models.StreamProfile, "objects",
                        MagicMock(only=MagicMock(return_value=fake_queryset)))
    assert gw._default_stream_profile_name() == "redirect"


def test_default_stream_profile_name_fails_safe_to_none_on_error(gw, monkeypatch):
    import sys

    core_models = sys.modules["core.models"]
    monkeypatch.setattr(core_models.CoreSettings, "get_default_stream_profile_id",
                        MagicMock(side_effect=RuntimeError("boom")))
    assert gw._default_stream_profile_name() is None


def test_default_stream_profile_name_returns_none_when_no_default_is_configured(gw,
                                                                                monkeypatch):
    import sys

    core_models = sys.modules["core.models"]
    monkeypatch.setattr(core_models.CoreSettings, "get_default_stream_profile_id",
                        MagicMock(return_value=None))
    assert gw._default_stream_profile_name() is None


def test_default_profile_is_redirect_uses_the_upstream_structural_test(gw, monkeypatch):
    """CoreSettings.is_default_stream_profile_redirect() is the same test
    Dispatcharr's proxy uses at play time; prefer it to re-deriving the
    verdict from the profile's name."""
    import sys

    core_models = sys.modules["core.models"]
    monkeypatch.setattr(core_models.CoreSettings,
                        "is_default_stream_profile_redirect",
                        MagicMock(return_value=True))
    assert gw._default_stream_profile_is_redirect() is True
    monkeypatch.setattr(core_models.CoreSettings,
                        "is_default_stream_profile_redirect",
                        MagicMock(return_value=False))
    assert gw._default_stream_profile_is_redirect() is False


def test_default_profile_is_redirect_fails_safe_to_none_on_error(gw, monkeypatch):
    import sys

    core_models = sys.modules["core.models"]
    monkeypatch.setattr(core_models.CoreSettings,
                        "is_default_stream_profile_redirect",
                        MagicMock(side_effect=RuntimeError("boom")))
    assert gw._default_stream_profile_is_redirect() is None


def test_default_profile_is_redirect_falls_back_to_the_name_heuristic(gw, monkeypatch):
    """An older Dispatcharr without the structural classmethod: resolve the
    default profile's NAME and apply the heuristic."""
    import sys

    core_models = sys.modules["core.models"]
    # A MagicMock auto-creates any attribute, so absence must be modeled with
    # a plain class, not delattr.
    monkeypatch.setattr(core_models, "CoreSettings", type("CS", (), {}))
    monkeypatch.setattr(gw, "_default_stream_profile_name", lambda: "redirect")
    assert gw._default_stream_profile_is_redirect() is True
    monkeypatch.setattr(gw, "_default_stream_profile_name", lambda: "hls proxy")
    assert gw._default_stream_profile_is_redirect() is False
    monkeypatch.setattr(gw, "_default_stream_profile_name", lambda: None)
    assert gw._default_stream_profile_is_redirect() is None


def test_channels_resolves_default_profile_once_and_applies_to_null_profile_rows(
        gw, monkeypatch):
    """The default must be resolved ONCE per report run (a single CoreSettings
    read), not once per channel row, and a NULL-profile channel must inherit
    that resolved verdict -- proven here with a default that resolves to
    Redirect, which must turn a NULL-profile channel unobservable."""
    import sys

    apps_models = sys.modules["apps.channels.models"]

    class FakeGroup:
        name = "US: News"

    class FakeChannel:
        def __init__(self, cid, profile):
            self.id = cid
            self.uuid = f"u{cid}"
            self.name = f"CH{cid}"
            self.channel_group = FakeGroup()
            self.stream_profile = profile
            self.auto_created = False
            self.created_at = None

    redirect_profile = type("P", (), {"name": "Redirect"})()
    rows_in = [FakeChannel(1, None), FakeChannel(2, redirect_profile)]

    class FakeQuerySet:
        def select_related(self, *a, **k):
            return self

        def only(self, *a, **k):
            return self

        def iterator(self, chunk_size=500):
            return iter(rows_in)

    monkeypatch.setattr(apps_models.Channel, "objects", FakeQuerySet())

    calls = []

    def fake_default():
        calls.append(1)
        return True                              # the default IS Redirect

    monkeypatch.setattr(gw, "_default_stream_profile_is_redirect", fake_default)

    result = gw.DjangoGateway().channels()
    assert len(calls) == 1                        # resolved ONCE per report run
    assert result[0].proxying is False            # NULL profile inherits Redirect
    assert result[1].proxying is False            # explicit Redirect profile
