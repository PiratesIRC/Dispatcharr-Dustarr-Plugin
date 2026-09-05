"""The settings form is divided into sections, and every setting sits under the right one.

Measured 2026-09-05 against the fields this plugin actually serves: 16 fields,
of which two were information panels and neither was a topical section heading.
`_section_quickstart` is an introduction telling the operator which button to
press first, and `info` states that the plugin is read only. After those, all
fourteen real settings ran as one flat list, from the collector's sampling
cadence to the report schedule, with nothing marking where one concern ended
and the next began.

That is a different fault from the one found in the sibling plugin
Stream-Mapparr, which had a single heading in the middle that nothing closed,
so every setting after it read as part of one narrow feature. Here there was no
heading to be wrong about. The remedy is the same mechanism: a field of type
"info" whose id begins with `_section_`.

ONE SETTING MOVED. `top_n` (Top/bottom N) sat between Cold threshold and
Never-watched alarm ceiling, which are both thresholds the report judges
against. It is not a threshold. It sets how many rows the Most used and Least
used tables show, so it moved to the section covering the report itself. Its id
is unchanged, so no saved value is affected.

These tests lock the section boundaries, so a setting added later cannot
silently land under the wrong heading.
"""
import io
import json
import os

import pytest
from conftest import find_invisible, find_non_ascii, load_plugin

load_plugin()


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module

MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dustarr", "plugin.json")

# Each section heading and the setting that must come directly after it.
# Locking the BOUNDARY rather than the full membership means adding a setting
# inside a section needs no test change, while moving a boundary does.
SECTION_BOUNDARIES = [
    ("_section_quickstart", "info"),
    ("_section_collection", "poll_interval_s"),
    ("_section_judging", "unused_threshold_days"),
    ("_section_exclusions", "exclude_auto_created"),
    ("_section_report", "top_n"),
]


def _fields(plugin):
    return plugin.Plugin.__new__(plugin.Plugin).fields


def _ids(plugin):
    return [f.get("id") for f in _fields(plugin)]


def _section_ids(plugin):
    return [f.get("id") for f in _fields(plugin)
            if str(f.get("id", "")).startswith("_section_")]


def _fields_under(plugin, section_id):
    """The field ids between `section_id` and the next section heading."""
    ids = _ids(plugin)
    start = ids.index(section_id) + 1
    out = []
    for fid in ids[start:]:
        if str(fid).startswith("_section_"):
            break
        out.append(fid)
    return out


# --------------------------------------------------------------------------- #
# The sections exist, in order, with nothing loose
# --------------------------------------------------------------------------- #
def test_every_expected_section_heading_is_served(plugin):
    served = _section_ids(plugin)
    missing = [s for s, _first in SECTION_BOUNDARIES if s not in served]
    assert not missing, f"missing section heading(s): {missing}"


def test_the_sections_appear_in_the_expected_order(plugin):
    assert _section_ids(plugin) == [s for s, _first in SECTION_BOUNDARIES]


@pytest.mark.parametrize("section_id,first_field", SECTION_BOUNDARIES)
def test_each_section_is_followed_by_the_setting_that_opens_it(
        plugin, section_id, first_field):
    """Locks where one section ends and the next begins."""
    ids = _ids(plugin)
    assert section_id in ids, f"{section_id} is not served at all"
    assert ids[ids.index(section_id) + 1] == first_field


def test_no_setting_sits_above_the_first_section_heading(plugin):
    ids = _ids(plugin)
    first = next(i for i, f in enumerate(ids) if str(f).startswith("_section_"))
    assert first == 0, f"{ids[:first]} appear before any heading"


def test_every_setting_belongs_to_some_section(plugin):
    """The measured fault was fourteen settings under no heading at all."""
    covered = set()
    for section_id, _first in SECTION_BOUNDARIES:
        covered.update(_fields_under(plugin, section_id))
    loose = [f for f in _ids(plugin)
             if not str(f).startswith("_section_") and f not in covered]
    assert loose == [], f"settings under no heading: {loose}"


# --------------------------------------------------------------------------- #
# The membership that matters
# --------------------------------------------------------------------------- #
def test_the_collection_section_holds_exactly_the_settings_the_collector_reads(plugin):
    """These four, and only these four, restart the collector when changed.

    `_thresholds_fingerprint` hashes `sessionizer.DEFAULTS`, so a change to one
    of them respawns the collector and forfeits any watch session in progress.
    A setting that does NOT do that must not sit here, because the section body
    tells the operator that everything under it has that cost.
    """
    assert set(_fields_under(plugin, "_section_collection")) == \
        set(plugin.sessionizer.DEFAULTS)


def test_the_row_count_setting_sits_with_the_report_settings(plugin):
    """`top_n` sets how many rows the rankings show. It is not a threshold, and
    it used to sit between two settings that are."""
    assert "top_n" in _fields_under(plugin, "_section_report")


def test_the_exclusion_section_holds_only_the_exclusion_settings(plugin):
    under = _fields_under(plugin, "_section_exclusions")
    strays = [f for f in under if not str(f).startswith("exclude_")]
    assert not strays, f"non-exclusion settings under the exclusions heading: {strays}"


def test_moving_the_row_count_setting_did_not_rename_it(plugin):
    """Dispatcharr never prunes a stored setting, and it is keyed on the id. A
    rename would strand the saved value and silently fall back to the default."""
    assert "top_n" in _ids(plugin)
    assert plugin.coerce_settings({"top_n": 5})["top_n"] == 5


# --------------------------------------------------------------------------- #
# Rules for text this plugin shows the operator
# --------------------------------------------------------------------------- #
def test_every_section_heading_has_a_body(plugin):
    """A heading with no body says only what its own words say, which is the
    field labels over again."""
    bare = [f.get("id") for f in _fields(plugin)
            if str(f.get("id", "")).startswith("_section_")
            and not (f.get("description") or "").strip()]
    assert bare == [], bare


def test_no_section_body_contains_a_line_break(plugin):
    """An info panel body is one flowing paragraph; line breaks are not safe there."""
    offenders = [f.get("id") for f in _fields(plugin)
                 if str(f.get("id", "")).startswith("_section_")
                 and "\n" in (f.get("description") or "")]
    assert offenders == [], offenders


def test_no_field_copy_uses_an_em_dash_or_a_double_hyphen(plugin):
    """Standing operator instruction. A double hyphen reads as an em dash."""
    em_dash = chr(0x2014)
    en_dash = chr(0x2013)
    offenders = []
    for f in _fields(plugin):
        for key in ("label", "description", "help_text"):
            text = f.get(key) or ""
            if em_dash in text or en_dash in text or "--" in text:
                offenders.append(f"{f.get('id')}.{key}")
    assert offenders == [], offenders


def test_all_field_copy_is_plain_ascii(plugin):
    """A settings form is rendered in a browser, but the same strings reach
    logs and files that may be read under a different codepage."""
    bad = {}
    for f in _fields(plugin):
        for key in ("label", "description", "help_text"):
            outside = find_non_ascii(f.get(key))
            if outside:
                bad[f"{f.get('id')}.{key}"] = outside
    assert bad == {}, bad


def test_no_field_copy_carries_an_invisible_character(plugin):
    """A zero width character or a line separator is unreviewable by eye.

    U+2028 and U+2029 are checked on the whole string on purpose: str.splitlines
    SPLITS on them, so a line by line search reports clean.
    """
    bad = {}
    for f in _fields(plugin):
        for key in ("label", "description", "help_text"):
            hits = find_invisible(f.get(key))
            if hits:
                bad[f"{f.get('id')}.{key}"] = hits
    assert bad == {}, bad


def test_section_headings_are_information_panels_that_store_nothing(plugin):
    """A heading must never become a stored setting: Dispatcharr never prunes one."""
    for f in _fields(plugin):
        if str(f.get("id", "")).startswith("_section_"):
            assert f.get("type") == "info", f["id"]
            assert "default" not in f, f["id"]


def test_the_manifest_declares_no_fields_so_it_cannot_drift(plugin):
    """plugin.py is the single source for the settings form.

    In Stream-Mapparr the two declarations had drifted apart and reading either
    alone gave a false picture of the interface. This plugin avoids that by
    declaring the form in exactly one place, and this test keeps it that way.
    """
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    assert not manifest.get("fields"), \
        "plugin.json has grown a fields list that can disagree with plugin.py"


# --------------------------------------------------------------------------- #
# The scanners themselves are proven, not assumed
# --------------------------------------------------------------------------- #
# Mutation testing on 2026-09-05: replacing the body of conftest.find_invisible
# and conftest.find_non_ascii with `return []` failed NO test, because the
# plugin's copy is clean and an assertion that something is absent cannot fail
# while it really is absent. A broken scanner and a clean plugin look identical.
# These tests plant a character and watch the scanner find it, so the scanner is
# never the vacuous half of the guard.
#
# Every character below is built with chr(). A literal invisible character in a
# source file is unreviewable by eye and a formatter can delete it silently.


@pytest.mark.parametrize("codepoint", sorted(find_invisible.__globals__["INVISIBLE_CODEPOINTS"]))
def test_the_invisible_character_scanner_finds_each_character_it_claims_to(codepoint):
    planted = "before" + chr(codepoint) + "after"
    assert find_invisible(planted) == [hex(codepoint)], \
        f"the scanner missed U+{codepoint:04X}"


def test_the_invisible_character_scanner_reports_nothing_for_ordinary_text():
    assert find_invisible("An ordinary sentence, with punctuation.") == []


def test_the_invisible_character_scanner_looks_past_a_line_separator():
    """str.splitlines() SPLITS on U+2028 and U+2029, so a scanner written line
    by line reports clean for the two characters most likely to cause trouble.
    A character AFTER a separator must still be found."""
    planted = "start" + chr(0x2028) + "middle" + chr(0x200B) + "end"
    assert find_invisible(planted) == sorted([hex(0x2028), hex(0x200B)])


def test_the_non_ascii_scanner_finds_a_character_outside_ascii():
    assert find_non_ascii("caf" + chr(0xE9)) == [hex(0xE9)]


def test_the_non_ascii_scanner_reports_nothing_for_plain_ascii():
    assert find_non_ascii("plain ascii text 123") == []
