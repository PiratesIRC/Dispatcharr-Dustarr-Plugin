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
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.MULTILINE))
    assert referenced <= defined, f"undefined: {referenced - defined}"
