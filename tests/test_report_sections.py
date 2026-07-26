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


def test_rollup_bar_cell_is_sortable(rp, gw):
    """_SORT_JS binds EVERY th and sorts on `dataset.v || textContent`. An
    SVG-only cell has neither, so the column would present as sortable and
    silently do nothing."""
    html = rp.render_html(model(rp, gw, n=5, watched=2))
    assert "never / judged" in html
    assert re.search(r'<td class="barcell" data-v="[0-9.]+"', html)


def test_rollup_group_name_is_escaped_in_the_svg_title(rp, gw):
    rows = [gw.ChannelRow(id=1, uuid="u1", name="CH", group='<b>G</b>',
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400,
                                      "coverage": {}}}
    html = rp.render_html(rp.build_model(rows, usage, SETTINGS, NOW))
    assert "<b>G</b>" not in html
    assert "&lt;b&gt;G&lt;/b&gt;" in html


def test_page_needs_no_script_for_structure_or_charts(rp, gw):
    """Charts and collapsing must survive scripting being disabled. The only
    script on the page is the progressive-enhancement column sorter."""
    html = rp.render_html(model(rp, gw))
    assert html.count("<script>") == 1
    body_start = html.index("<body>")
    script_start = html.index("<script>")
    # Everything structural is emitted before the sorter, and none of it
    # depends on the sorter running.
    assert "<details" in html[body_start:script_start]
    assert "<svg" in html[body_start:script_start]


def test_no_colour_presentation_attributes_anywhere_in_the_page(rp, gw):
    """`fill="var(--x)"` has patchy support and falls back to BLACK, an
    invisible chart on the dark #14161a surface -- this must hold across the
    WHOLE page, not just the split bar / meter / mini-bar generators."""
    html = rp.render_html(model(rp, gw))
    assert 'fill="' not in html
    assert 'stroke="' not in html


def test_render_is_newline_agnostic(rp, gw):
    """CRLF worktree, LF index, LF CI -- assertions must not depend on it."""
    html = rp.render_html(model(rp, gw))
    assert html.replace("\r\n", "\n").count("<details") == 6


def test_regenerate_the_committed_fixture(rp, gw, tmp_path):
    """The visual check leaves an artifact a later session can diff, rather
    than an unfalsifiable `I looked at it`."""
    import pathlib
    html = rp.render_html(model(rp, gw, n=12, watched=4))
    fixture = (pathlib.Path(__file__).parent / "fixtures" / "sample_report.html")
    assert fixture.exists(), (
        "tests/fixtures/sample_report.html is missing -- regenerate it with: "
        "python scripts/render_sample.py")
    on_disk = fixture.read_text(encoding="utf-8")
    assert on_disk.replace("\r\n", "\n") == html.replace("\r\n", "\n"), (
        "tests/fixtures/sample_report.html is stale (renderer changed) -- "
        "regenerate and commit it with: python scripts/render_sample.py")
