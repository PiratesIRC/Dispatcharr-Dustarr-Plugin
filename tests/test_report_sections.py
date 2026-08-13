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
    "Channels going cold": False,
}


def _summary_text(summary):
    """The words in a summary line, with markup and decoration removed.

    Stripping tags is not enough any more: a section heading carries an
    aria-hidden emoji INSIDE a span, so the tag strip leaves the glyph behind
    and every `startswith(title)` check fails. Drop anything before the first
    ASCII letter, which is where the real title starts."""
    text = re.sub(r"<[^>]+>", "", summary)
    return re.sub(r"^[^A-Za-z]+", "", text).strip()


def _sections(html):
    """Map section title -> the raw `<details ...>` open tag that introduces it."""
    out = {}
    for tag, summary in re.findall(r"(<details[^>]*>)\s*<summary>(.*?)</summary>",
                                   html, re.DOTALL):
        text = _summary_text(summary)
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


def test_every_section_carries_a_count(rp, gw):
    """`Least used` / `Most used` used to be the only two sections whose size
    the report would not tell you, on the reasoning that a top-N slice is not
    a population. The reader cannot see that distinction from a collapsed
    summary line -- they see two sections that forgot their number."""
    html = rp.render_html(model(rp, gw, n=8, watched=6))
    found = re.findall(r"<summary>(.*?)</summary>", html, re.DOTALL)
    assert len(found) == len(EXPECTED_OPEN)
    for summary in found:
        text = re.sub(r"<[^>]+>", "", summary).strip()
        assert 'class="count"' in summary, text


def test_usage_section_counts_equal_the_rows_rendered_beneath_them(rp, gw):
    """The count span means the same thing in every section: how many rows are
    in the table directly below it. For the two ranking sections that is the
    length of the slice, never the size of the judged population it came
    from."""
    built = model(rp, gw, n=8, watched=6)
    html = rp.render_html(built)
    for title, key in (("Least used", "least_used"), ("Most used", "most_used")):
        match = re.search(
            rf'{title} <span class="count">(\d+)</span></summary>(.*?)</details>',
            html, re.DOTALL)
        assert match, title
        assert int(match.group(1)) == len(built[key]), title
        # "<tr><td" is a BODY row; the header row is "<tr><th".
        assert match.group(2).count("<tr><td") == len(built[key]), title


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
    assert html.replace("\r\n", "\n").count("<details") == 7


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


def test_the_cold_section_states_a_shortened_window(rp, gw):
    """Empty because the dataset is young must not read as empty because
    nothing is cold."""
    model_ = model(rp, gw)
    model_["cold_window_clamped"] = True
    model_["cold_window_days"] = 25.0
    html = rp.render_html(model_)
    assert "25.0 days" in html
    assert "as far back as this dataset goes" in html


def test_the_cold_section_separates_still_tried_channels(rp, gw):
    model_ = model(rp, gw)
    model_["cold_still_tried"] = model_["most_used"][:1]
    html = rp.render_html(model_)
    assert "still trying these" in html


def test_the_cold_section_count_matches_both_tables(rp, gw):
    model_ = model(rp, gw)
    model_["cold_abandoned"] = model_["most_used"][:1]
    model_["cold_still_tried"] = model_["most_used"][1:2]
    html = rp.render_html(model_)
    assert 'Channels going cold <span class="count">2</span>' in html


def test_classification_and_rendering_agree_end_to_end(rp, gw):
    """No other test drives build_model() and render_html() together for the
    cold section -- the tests above hand-mutate a model dict instead. Build a
    real model from channel rows and usage data holding two genuinely cold
    channels (watched once, long ago, never tuned since), render it, and
    check the section heading count and that both channel names land inside
    the "Channels going cold" section and nowhere else on the page.
    top_n=0 keeps most_used/least_used empty so a cold channel cannot also
    surface there and make the "nowhere else" half of this test meaningless.
    """
    rows = [gw.ChannelRow(id=0, uuid="cold-a", name="Cold Channel Alpha",
                          group="US: Movies", auto_created=False,
                          created_at=NOW - 300 * 86400, proxying=True),
            gw.ChannelRow(id=1, uuid="cold-b", name="Cold Channel Bravo",
                          group="US: Movies", auto_created=False,
                          created_at=NOW - 300 * 86400, proxying=True)]
    channels = {
        "cold-a": {"watch_count": 2, "watch_seconds": 3600.0, "tune_count": 2,
                  "last_watched": NOW - 100 * 86400,
                  "last_tuned": NOW - 100 * 86400,
                  "first_seen": NOW - 250 * 86400},
        "cold-b": {"watch_count": 1, "watch_seconds": 1800.0, "tune_count": 1,
                  "last_watched": NOW - 90 * 86400,
                  "last_tuned": NOW - 90 * 86400,
                  "first_seen": NOW - 250 * 86400},
    }
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 200 * 86400, "coverage": {}}}
    built = rp.build_model(rows, usage, dict(SETTINGS, top_n=0), NOW)
    assert [e["uuid"] for e in built["cold_abandoned"]] == ["cold-a", "cold-b"]
    assert built["cold_still_tried"] == []

    html = rp.render_html(built)
    found = _sections(html)
    assert "Channels going cold" in found
    assert 'Channels going cold <span class="count">2</span>' in html

    match = re.search(r'<details[^>]*>\s*<summary>.*?Channels going cold.*?'
                      r'</summary>(.*?)</details>', html, re.DOTALL)
    assert match, "could not locate the going-cold section body"
    cold_body = match.group(1)
    assert "Cold Channel Alpha" in cold_body
    assert "Cold Channel Bravo" in cold_body

    rest_of_page = html.replace(cold_body, "", 1)
    assert "Cold Channel Alpha" not in rest_of_page
    assert "Cold Channel Bravo" not in rest_of_page
