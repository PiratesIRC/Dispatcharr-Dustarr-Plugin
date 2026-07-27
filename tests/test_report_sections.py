import re

from conftest import NOW, SETTINGS, load_plugin, model

load_plugin()


# Spec section 4.2, amended 2026-07-27: every section now starts COLLAPSED at
# the operator's request, so the page opens as an index rather than a wall of
# tables. Title -> expected `open` attribute present.
EXPECTED_OPEN = {
    "Never watched": False,
    "Too new to judge": False,
    "Tuned but never qualified": False,
    "Least used": False,
    "Most used": False,
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


# `%Y-%m-%d %H:%M %Z` rendered through time.localtime(): both the hour and the
# zone name depend on the machine. The committed fixture is generated on
# whichever box ran the script, so comparing these verbatim makes the test pass
# only in the generating machine's timezone. CI runs in UTC and could never
# pass. Mask them; everything else in the page is still compared byte for byte.
_LOCAL_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Za-z][A-Za-z ]*")


def _tz_agnostic(text):
    return _LOCAL_STAMP.sub("<LOCAL-TIMESTAMP>", text.replace("\r\n", "\n"))


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
    assert _tz_agnostic(on_disk) == _tz_agnostic(html), (
        "tests/fixtures/sample_report.html is stale (renderer changed) -- "
        "regenerate and commit it with: python scripts/render_sample.py")


def test_the_fixture_comparison_is_timezone_agnostic(rp, gw):
    """The mask above is the only reason this suite can pass off this box.

    Prove it actually neutralises a zone difference rather than happening to
    match: rewrite a rendered page as if it came from a UTC machine, and the
    comparison must still hold.
    """
    html = rp.render_html(model(rp, gw, n=12, watched=4))
    assert _LOCAL_STAMP.search(html), "no local timestamp in the page to mask"
    as_utc = _LOCAL_STAMP.sub("1999-01-01 00:00 UTC", html)
    assert as_utc != html
    assert _tz_agnostic(as_utc) == _tz_agnostic(html)


def test_every_section_starts_collapsed(rp, gw):
    """The page opens as an index, not a wall of tables. `<details>` with no
    `open` is the whole mechanism, so a stray `open` is the only way this
    regresses."""
    html = rp.render_html(model(rp, gw))
    assert "<details open>" not in html
    assert "<details >" not in html
    assert html.replace("\r\n", "\n").count("<details>") == len(EXPECTED_OPEN)


def test_every_collapsed_section_carries_the_find_in_page_hint(rp, gw):
    """find-in-page does not reach inside a collapsed <details> on some
    browsers. Every section is collapsed, so every section must say so."""
    html = rp.render_html(model(rp, gw))
    bodies = re.findall(r"</summary>(.*?)</details>", html, re.DOTALL)
    assert len(bodies) == len(EXPECTED_OPEN)
    for body in bodies:
        assert "find-in-page" in body.lower()


def test_every_section_carries_a_description(rp, gw):
    """Every section is collapsed, so the summary line plus one short
    description is all the reader has to decide whether to expand. The
    rankings/least-used notes are CONDITIONAL, so they cannot stand in for
    this: build a model where neither fires."""
    built = model(rp, gw, n=5, watched=5)
    html = rp.render_html(built)
    bodies = re.findall(r"</summary>(.*?)</details>", html, re.DOTALL)
    assert len(bodies) == len(EXPECTED_OPEN)
    for title, body in zip(_sections(html), bodies, strict=True):
        assert "<p class='sub'>" in body, title


def test_report_copy_has_no_em_dashes_or_contractions(rp, gw):
    """Standing operator instruction. `--` reads as an em dash in rendered
    text, so it counts; comments and docstrings are not rendered and are out
    of scope here."""
    html = rp.render_html(model(rp, gw))
    body = html[html.index("<body>"):]
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ",
                  body, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", body)
    assert "â€”" not in text
    assert "â€“" not in text
    assert "--" not in text
    found = re.findall(r"\b\w+['â€™]\w+\b", text)
    assert not found, found
