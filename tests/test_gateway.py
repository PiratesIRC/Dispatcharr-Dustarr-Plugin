import pytest
from conftest import load_plugin

load_plugin()  # installs the Django stubs before gateway imports anything


@pytest.fixture()
def gw():
    import sys
    return sys.modules["metricsarr_under_test.gateway"]


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
