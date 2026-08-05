import re

import pytest
from conftest import load_plugin

load_plugin()

NOW = 1_700_000_000.0

# Pinned by docs/superpowers/specs/2026-07-26-report-visual-polish-design.md
# section 6. Validated all-pairs with the dataviz skill's validate_palette.js
# against surfaces #fbfbfd (light) and #14161a (dark). Do NOT edit either
# column without re-running that validator: orange was rejected for
# never-watched because orange<->red falls below the normal-vision floor of 15
# in both modes (7.1 light, 13.0 dark).
PALETTE_LIGHT = {"--never": "#2a78d6", "--watched": "#1baf7a",
                 "--tuned": "#e34948", "--toonew": "#898781",
                 "--track": "#e1e0d9",
                 "--ok": "#0ca30c", "--bad": "#d03b3b"}
PALETTE_DARK = {"--never": "#3987e5", "--watched": "#199e70",
                "--tuned": "#e66767", "--toonew": "#898781",
                "--track": "#2c2c2a",
                "--ok": "#0ca30c", "--bad": "#d03b3b"}


@pytest.fixture()
def rp():
    import sys
    return sys.modules["dustarr_under_test.reports"]


def _dark_block(css):
    start = css.index("prefers-color-scheme: dark")
    return css[start:]


def test_css_defines_the_light_palette(rp):
    css = rp._CSS
    light = css[:css.index("prefers-color-scheme: dark")]
    for var, hex_value in PALETTE_LIGHT.items():
        assert f"{var}: {hex_value}" in light, f"{var} missing or wrong in light"


def test_css_redeclares_every_var_for_dark(rp):
    dark = _dark_block(rp._CSS)
    for var, hex_value in PALETTE_DARK.items():
        assert f"{var}: {hex_value}" in dark, f"{var} missing or wrong in dark"


def test_every_var_referenced_is_defined(rp):
    """A var(--x) with no definition silently resolves to nothing -- for a
    fill that means BLACK, i.e. an invisible chart on the dark surface."""
    css = rp._CSS
    referenced = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    defined = set(re.findall(r"(--[a-z0-9-]+):", css))
    assert referenced <= defined, f"undefined: {referenced - defined}"


def _rect_widths(svg):
    return [float(w) for w in re.findall(r'<rect[^>]*class="seg[^"]*"[^>]*width="([0-9.]+)"', svg)]


def test_segment_widths_sum_to_track_minus_gaps(rp):
    segs = [("never", 385, "seg-never"), ("watched", 25, "seg-watched"),
            ("tuned", 20, "seg-tuned")]
    svg, _ = rp._svg_split_bar(segs, width=900)
    widths = _rect_widths(svg)
    assert len(widths) == 3
    expected = 900 - rp.GAP_PX * (len(widths) - 1)
    assert abs(sum(widths) - expected) <= 1.0


def test_segments_are_proportional_above_the_floor(rp):
    segs = [("a", 300, "seg-never"), ("b", 100, "seg-watched")]
    svg, _ = rp._svg_split_bar(segs, width=900)
    wide, narrow = _rect_widths(svg)
    assert abs(wide / narrow - 3.0) < 0.05


def test_zero_count_segments_are_dropped_entirely(rp):
    """A zero segment must emit no rect, no gap and no legend entry -- and
    dropping it is why the palette is validated ALL-PAIRS, since any two
    segments can become neighbours."""
    segs = [("never", 385, "seg-never"), ("toonew", 0, "seg-toonew"),
            ("tuned", 20, "seg-tuned")]
    svg, legend = rp._svg_split_bar(segs, width=900)
    assert len(_rect_widths(svg)) == 2
    assert "toonew" not in legend
    assert abs(sum(_rect_widths(svg)) - (900 - rp.GAP_PX)) <= 1.0


def test_gate_pct_tracks_gates_min_coverage(rp):
    """GATE_PCT must be READ from gates.MIN_COVERAGE, not a second hardcoded
    0.90 -- otherwise the tick position and the "gate at N%" title text can
    silently disagree with the actual gate if it ever moves."""
    import sys
    gates = sys.modules["dustarr_under_test.gates"]
    assert rp.GATE_PCT == gates.MIN_COVERAGE


def test_meter_clamps_out_of_range_fractions(rp):
    """`coverage` is a FRACTION, not a percentage. Clamp to [0.0, 1.0] --
    clamping to [0,100] would let 1.4 through as a 1.4%-full bar."""
    for bad in (1.4, -0.2, float("inf")):
        svg, _ = rp._svg_meter(bad, True, width=280)
        assert "NaN" not in svg
        fills = [float(w) for w in
                 re.findall(r'<rect[^>]*class="fill"[^>]*width="([0-9.]+)"', svg)]
        assert fills and 0.0 <= fills[0] <= 280.0


def test_meter_length_is_coverage_and_colour_is_not_the_gate(rp):
    """Length = sampling density; the gate verdict rides on a SEPARATE chip.
    A full green bar would read `trustworthy` -- exactly the picture a
    blind-but-ticking collector produces."""
    ok_svg, ok_chip = rp._svg_meter(0.96, True, width=280)
    bad_svg, bad_chip = rp._svg_meter(0.96, False, width=280)
    assert ok_svg == bad_svg, "the meter must not encode the gate verdict"
    assert ok_chip != bad_chip


def test_meter_chip_carries_words_never_colour_alone(rp):
    _, ok_chip = rp._svg_meter(0.96, True)
    _, bad_chip = rp._svg_meter(0.4, False)
    assert "sampling OK" in ok_chip
    assert "not trustworthy" in bad_chip


def test_meter_marks_the_gate_threshold(rp):
    """Without a tick, 89% and 4% both render as `red` and distance-to-gate
    is lost."""
    svg, _ = rp._svg_meter(0.5, False, width=280)
    ticks = re.findall(r'class="tick"[^>]*x="([0-9.]+)"', svg)
    assert ticks, "no gate tick"
    assert abs(float(ticks[0]) - 280 * rp.GATE_PCT) < 1.0


def test_meter_emits_no_colour_presentation_attributes(rp):
    svg, _ = rp._svg_meter(0.5, True)
    assert "fill=" not in svg
    assert "stroke=" not in svg


def test_tiny_nonzero_segment_is_floored_to_stay_visible(rp):
    segs = [("big", 1439, "seg-never"), ("tiny", 1, "seg-tuned")]
    svg, _ = rp._svg_split_bar(segs, width=900)
    widths = _rect_widths(svg)
    assert min(widths) >= rp.MIN_SEG_PX
    assert abs(sum(widths) - (900 - rp.GAP_PX)) <= 1.0


def test_empty_input_renders_an_empty_track_not_a_crash(rp):
    svg, legend = rp._svg_split_bar([], width=900)
    assert "Nothing judged yet" in svg
    assert "NaN" not in svg
    for zero_total in ([("a", 0, "seg-never")],):
        svg2, _ = rp._svg_split_bar(zero_total, width=900)
        assert "NaN" not in svg2


def test_split_bar_has_no_in_bar_text_label_at_any_width(rp):
    """`.chart { width: 100% }` over a fixed-height viewBox with
    `preserveAspectRatio="none"` scales X but not Y, so any in-bar <text>
    stretches on desktop and squashes on a narrow phone screen (the report
    is emailed and opened on phones) -- there is no width at which it
    renders undistorted. Every count already appears, undistorted, in the
    legend and in a table on the same page, so the bar itself must carry no
    text at all, regardless of segment size or bar width."""
    segs = [("never", 1010, "seg-never"), ("tuned", 5, "seg-tuned")]
    for width in (900, 320):
        svg, legend = rp._svg_split_bar(segs, width=width)
        assert "seglabel" not in svg
        assert "<text" not in svg
        assert ">1010<" in legend
        assert ">5<" in legend


def test_generators_emit_no_colour_presentation_attributes(rp):
    """fill="var(--x)" has patchy support and falls back to BLACK on failure --
    an invisible chart on the dark surface. Colour comes from CSS rules only."""
    segs = [("never", 385, "seg-never"), ("watched", 25, "seg-watched")]
    svg, _ = rp._svg_split_bar(segs, width=900)
    assert "fill=" not in svg
    assert "stroke=" not in svg


def test_segment_order_is_pinned(rp):
    """Section 6: order is chosen for reading, but the hexes were validated as
    a SET. Re-run validate_palette.js before changing either."""
    assert rp._SEGMENT_ORDER == (
        ("Never watched", "never_watched", "seg-never"),
        ("Watched", "watched", "seg-watched"),
        ("Too new", "too_new", "seg-toonew"),
        ("Tuned, never qualified", "tuned_never_qualified", "seg-tuned"),
    )


# Fix round 1: hostile-input coercion (Finding 1) and debt-redistribution
# overflow (Finding 2). Both are against the function's documented "TOTAL
# over its inputs" contract -- write_report catches only OSError, so
# anything else escapes to the action layer.

def test_hostile_counts_never_raise_and_are_dropped(rp):
    """`count or 0` let float('nan') (truthy!) and non-numeric strings reach
    int() and raise. `float('inf')`/`float('-inf')` slip past the NaN guard
    (infinity equals itself) and raise `OverflowError` instead -- a third
    exception type, caught alongside TypeError/ValueError. Every hostile
    value here must degrade to a dropped segment, never an exception."""
    segs = [
        ("never", 385, "seg-never"),
        ("negative", -5, "seg-watched"),
        ("none", None, "seg-tuned"),
        ("nonnumeric", "not-a-number", "seg-toonew"),
        ("nan", float("nan"), "seg-watched"),
        ("posinf", float("inf"), "seg-tuned"),
        ("neginf", float("-inf"), "seg-toonew"),
    ]
    svg, legend = rp._svg_split_bar(segs, width=900)
    widths = _rect_widths(svg)
    assert len(widths) == 1          # only "never" survives
    assert "NaN" not in svg
    assert "nan" not in legend
    assert "negative" not in legend
    assert "none" not in legend
    assert "nonnumeric" not in legend
    assert "posinf" not in legend
    assert "neginf" not in legend


def test_all_zero_counts_render_empty_track(rp):
    svg, legend = rp._svg_split_bar(
        [("a", 0, "seg-never"), ("b", 0, "seg-watched")], width=900)
    assert "Nothing judged yet" in svg
    assert "NaN" not in svg
    assert legend == ""


def test_single_segment_fills_the_track(rp):
    svg, _ = rp._svg_split_bar([("only", 42, "seg-never")], width=900)
    widths = _rect_widths(svg)
    assert len(widths) == 1
    assert abs(widths[0] - 900) <= 1.0


def test_width_zero_does_not_raise(rp):
    svg, _ = rp._svg_split_bar(
        [("a", 10, "seg-never"), ("b", 5, "seg-watched")], width=0)
    assert "NaN" not in svg


def test_many_tiny_segments_never_overflow_the_track(rp):
    """Degenerate-branch cover: 300 tiny segments plus one huge one at
    width=900 makes track (300) < n*MIN_SEG_PX (602), so `_fit_to_track`
    takes its even-split fallback rather than the proportional-shedding
    loop. Still must never overflow the track (the old single-absorber bug
    overflowed this shape by ~300px) or go negative."""
    segs = [("huge", 100_000, "seg-never")]
    segs += [(f"tiny{i}", 1, "seg-watched") for i in range(300)]
    svg, _ = rp._svg_split_bar(segs, width=900)
    widths = _rect_widths(svg)
    assert len(widths) == 301
    track = 900 - rp.GAP_PX * (len(widths) - 1)
    # Tolerance matches the rest of this suite (e.g.
    # test_segment_widths_sum_to_track_minus_gaps): rendered widths are
    # individually rounded to 2 decimals for the SVG attribute, so summing
    # 301 of them can accumulate a little display-rounding slop. The old
    # single-absorber bug overflowed by ~300px on this exact shape -- far
    # past this tolerance -- so it still fails the invariant.
    assert sum(widths) <= track + 1.0
    assert all(w >= 0 for w in widths)


def test_multi_donor_redistribution_loop_runs_and_holds_the_invariant(rp):
    """Finding 3: the previous stress test lands in the DEGENERATE even-split
    branch (track < n*floor) and never exercises the proportional multi-donor
    shedding loop in `_fit_to_track`. This shape is the reviewer's own
    non-degenerate repro: 3 segments of 1000 plus 300 of 1, width=2000 ->
    track=1396, n*floor=606 (track >= n*floor, so the loop runs), and no
    single one of the three big segments has enough headroom alone to absorb
    the floor debt from 300 tiny segments -- multiple donors must share it."""
    segs = [(f"big{i}", 1000, "seg-never") for i in range(3)]
    segs += [(f"tiny{i}", 1, "seg-watched") for i in range(300)]
    svg, _ = rp._svg_split_bar(segs, width=2000)
    widths = _rect_widths(svg)
    assert len(widths) == 303
    track = 2000 - rp.GAP_PX * (len(widths) - 1)
    assert track >= 303 * rp.MIN_SEG_PX  # confirms the non-degenerate branch
    assert sum(widths) <= track + 1.0
    assert all(w >= rp.MIN_SEG_PX - 1.0 for w in widths)
    assert all(w >= 0 for w in widths)


def test_mini_bar_is_proportional_to_judged_not_total(rp):
    svg = rp._svg_mini_bar(3, 4, "US: Movies")
    fills = [float(w) for w in
             re.findall(r'<rect[^>]*class="fill"[^>]*width="([0-9.]+)"', svg)]
    assert len(fills) == 1
    assert abs(fills[0] / 100.0 - 0.75) < 0.01


def test_mini_bar_guards_a_zero_denominator(rp):
    svg = rp._svg_mini_bar(0, 0, "G")
    assert "NaN" not in svg
    assert "<svg" in svg


def test_mini_bar_clamps_an_impossible_ratio(rp):
    svg = rp._svg_mini_bar(9, 4, "G")
    fills = [float(w) for w in
             re.findall(r'<rect[^>]*class="fill"[^>]*width="([0-9.]+)"', svg)]
    assert fills[0] <= 100.0


def test_mini_bar_escapes_the_group_name_in_its_title(rp):
    """The group name is provider-controlled and is the ONLY such string that
    reaches an SVG."""
    svg = rp._svg_mini_bar(1, 2, '<script>alert(1)</script>')
    assert "<script>alert" not in svg
    assert "&lt;script&gt;" in svg


def test_mini_bar_emits_no_colour_presentation_attributes(rp):
    svg = rp._svg_mini_bar(1, 2, "G")
    assert "fill=" not in svg
    assert "stroke=" not in svg


def test_meter_survives_a_huge_int_fraction(rp):
    """`float(10**400)` raises OverflowError, not TypeError/ValueError -- the
    exact third exception type _coerce_segment_count already guards against
    and _svg_meter's own `float()` call had not ported. The docstring claims
    TOTAL over its inputs; this is the case that used to break that claim."""
    svg, _ = rp._svg_meter(10 ** 400, True, width=280)
    assert "NaN" not in svg
    fills = [float(w) for w in
             re.findall(r'<rect[^>]*class="fill"[^>]*width="([0-9.]+)"', svg)]
    assert fills and 0.0 <= fills[0] <= 280.0


# -- FIX 4: SVG geometry constants must agree with their unpinned CSS -------
#
# `preserveAspectRatio="none"` means a change to the Python constant alone,
# with no matching CSS edit, silently stretches the rendered SVG. Parse the
# literal out of _CSS and compare it to the constant rather than restructure
# the CSS to interpolate it (a test is enough, and less fragile).

def test_bar_height_constant_matches_the_css(rp):
    m = re.search(r"\.chart\s*\{[^}]*height:\s*(\d+)px", rp._CSS)
    assert m, "no .chart height rule found in _CSS"
    assert int(m.group(1)) == rp.BAR_H


def test_meter_height_constant_matches_the_css(rp):
    m = re.search(r"\.meter\s*\{[^}]*height:\s*(\d+)px", rp._CSS)
    assert m, "no .meter height rule found in _CSS"
    assert int(m.group(1)) == rp.METER_H


def test_mini_dimension_constants_match_the_css(rp):
    m = re.search(r"\.mini\s*\{[^}]*width:\s*(\d+)px[^}]*height:\s*(\d+)px", rp._CSS)
    assert m, "no .mini width/height rule found in _CSS"
    assert int(m.group(1)) == rp.MINI_W
    assert int(m.group(2)) == rp.MINI_H


# -- Refactoring UI pass (2026-08-05): the token layer, and what may break it -
#
# Two rules from github.com/LovroPodobnik/refactoring-ui-skill are now
# structural rather than stylistic, so they get guards. Both regress the same
# way: someone adds one more rule with a hand-picked value, it looks fine, and
# the system quietly stops being a system.

# Everything after the `body {` rule is page styling rather than token
# declarations. Colours and spacing there must come from var(), not literals.
#
# COMMENTS ARE STRIPPED FIRST, and that is load-bearing rather than tidiness:
# these rules are explained in prose that necessarily quotes the very things
# they forbid ("8px/12px rather than the old 6px/10px", the two chip hexes).
# Without the strip, every guard below fires on its own documentation and the
# only way to make them pass is to delete the explanation.
def _rule_body(css):
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return without_comments[without_comments.index("body {"):]


def test_no_rule_hardcodes_a_colour_outside_the_token_block(rp):
    """A literal hex in a rule is a colour that light mode and dark mode
    cannot both be right about. That is exactly how the old dark block ended
    up with three `!important` overrides."""
    stray = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", _rule_body(rp._CSS))))
    assert not stray, f"hardcode a token instead of these literals: {stray}"


def test_every_spacing_value_comes_from_the_scale(rp):
    """Margins, paddings and gaps pick a step off --s1..--s5. Fourteen one-off
    values preceded this. Sizes (widths, heights, font sizes, radii, borders)
    are NOT spacing and are deliberately out of scope."""
    offenders = []
    for prop, value in re.findall(r"\b(margin|padding|gap)\s*:\s*([^;}]+)",
                                  _rule_body(rp._CSS)):
        for token in value.split():
            if token.endswith("px") and token not in ("0", "0px"):
                offenders.append(f"{prop}: {token}")
    assert not offenders, f"off-scale spacing, use var(--sN): {offenders}"


def test_text_hierarchy_uses_ink_tokens_not_opacity(rp):
    """`opacity` paints a different colour on every surface it lands on, so
    the contrast ratio moves whenever a background changes, and the fade
    applies to everything nested inside. The one survivor is a fill blend on a
    decorative SVG tick, which is not text."""
    faded = re.findall(r"([^{}]+)\{[^}]*opacity:", _rule_body(rp._CSS))
    assert [s.strip() for s in faded] == [".meter .tick"], faded


def test_the_measured_ink_ramp_is_pinned(rp):
    """Measured against the two validated surfaces: --ink-dim is the weakest
    at 5.24:1 on light and 6.89:1 on dark, both clear of the 4.5:1 floor for
    normal text. Changing a value here without re-measuring puts text below
    it with nothing to say so."""
    css = rp._CSS
    light = css[:css.index("prefers-color-scheme: dark")]
    dark = css[css.index("prefers-color-scheme: dark"):]
    for var, value in (("--ink", "#16181d"), ("--ink-muted", "#5c616b"),
                       ("--ink-dim", "#656a76")):
        assert f"{var}: {value}" in light, f"{var} changed in light"
    for var, value in (("--ink", "#e8eaed"), ("--ink-muted", "#a7adb8"),
                       ("--ink-dim", "#9aa0ab")):
        assert f"{var}: {value}" in dark, f"{var} changed in dark"
