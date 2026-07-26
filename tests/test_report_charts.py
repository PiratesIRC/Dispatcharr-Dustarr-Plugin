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
                 "--track": "#e1e0d9", "--gap": "#ffffff",
                 "--ok": "#0ca30c", "--bad": "#d03b3b"}
PALETTE_DARK = {"--never": "#3987e5", "--watched": "#199e70",
                "--tuned": "#e66767", "--toonew": "#898781",
                "--track": "#2c2c2a", "--gap": "#1a1d22",
                "--ok": "#0ca30c", "--bad": "#d03b3b"}


@pytest.fixture()
def rp():
    import sys
    return sys.modules["metricsarr_under_test.reports"]


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


def test_label_fit_is_measured_not_a_share_threshold(rp):
    """A share threshold breaks at narrow widths: 6% is 54px at 900 but 19px
    at 320, while '1010' needs ~28px."""
    segs = [("never", 1010, "seg-never"), ("tuned", 5, "seg-tuned")]
    wide, _ = rp._svg_split_bar(segs, width=900)
    narrow, _ = rp._svg_split_bar(segs, width=320)
    assert ">1010<" in wide
    assert ">5<" not in wide          # 5/1015 of 900px cannot hold one digit
    assert ">5<" not in narrow


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
