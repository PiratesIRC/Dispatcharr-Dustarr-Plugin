import re

import pytest
from conftest import load_plugin

load_plugin()

NOW = 1_700_000_000.0

SETTINGS = {"exclude_groups": "", "exclude_name_regex": "",
            "exclude_auto_created": False, "top_n": 5,
            "unused_threshold_days": 30, "never_watched_ceiling": 0.99,
            "poll_interval_s": 15.0, "client_gap_grace_s": 90.0}


@pytest.fixture()
def rp():
    import sys
    return sys.modules["metricsarr_under_test.reports"]


@pytest.fixture()
def gw():
    import sys
    return sys.modules["metricsarr_under_test.gateway"]


def model(rp, gw, n=5, watched=2):
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True) for i in range(n)]
    channels = {f"u{i}": {"watch_count": 3, "watch_seconds": 7200.0,
                          "tune_count": 3, "last_watched": NOW - 3600,
                          "last_tuned": NOW - 3600,
                          "first_seen": NOW - 80 * 86400}
                for i in range(watched)}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    return rp.build_model(rows, usage, SETTINGS, NOW)


# Spec section 4.2. Title -> expected `open` attribute present.
EXPECTED_OPEN = {
    "Never watched": True,
    "Too new to judge": False,
    "Tuned but never qualified": True,
    "Least used": False,
    "Most used": True,
    "Excluded and unobservable": False,
}


def _sections(html):
    """Map section title -> the raw `<details ...>` open tag that introduces it."""
    out = {}
    for tag, summary in re.findall(r"(<details[^>]*>)\s*<summary>(.*?)</summary>",
                                   html, re.DOTALL):
        text = re.sub(r"<[^>]+>", "", summary).strip()
        for title in EXPECTED_OPEN:
            if text.startswith(title):
                out[title] = tag
    return out


def test_every_section_is_a_details_with_the_specified_default_state(rp, gw):
    found = _sections(rp.render_html(model(rp, gw)))
    assert set(found) == set(EXPECTED_OPEN), f"missing: {set(EXPECTED_OPEN) - set(found)}"
    for title, should_be_open in EXPECTED_OPEN.items():
        assert ("open" in found[title]) is should_be_open, title


def test_empty_sections_still_render_with_their_default_state(rp, gw):
    """too_new / tuned / unobservable are all realistically 0 on this box.
    Emptiness must never change the open/closed default."""
    built = model(rp, gw, n=5, watched=5)
    assert built["counts"]["too_new"] == 0
    found = _sections(rp.render_html(built))
    assert "Too new to judge" in found
    assert "open" not in found["Too new to judge"]


def test_usage_sections_carry_no_count(rp, gw):
    """`Least used` / `Most used` are top-N slices, not populations."""
    html = rp.render_html(model(rp, gw))
    found = re.findall(r"<summary>(.*?)</summary>", html, re.DOTALL)
    for summary in found:
        text = re.sub(r"<[^>]+>", "", summary).strip()
        if text.startswith(("Least used", "Most used")):
            assert "class=\"count\"" not in summary, text


def test_banner_precedes_every_collapsible_section(rp, gw):
    """The gate banner is the loudest honesty signal on the page; it must
    never be collapsible or sit inside a collapsed section."""
    html = rp.render_html(model(rp, gw, n=5, watched=0))
    assert "banner" in html
    assert html.index("banner") < html.index("<details")


def test_no_content_is_lost_to_a_details_unaware_client(rp, gw):
    """The failure mode of <details> must be `everything visible`, never
    `content lost` -- every row is in the DOM regardless of open state."""
    html = rp.render_html(model(rp, gw, n=5, watched=2))
    for i in range(5):
        assert f">CH{i}<" in html
