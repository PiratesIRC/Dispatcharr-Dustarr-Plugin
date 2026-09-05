"""Action button colour, and the manifest not being able to disagree about it.

Measured 2026-09-05 against the four actions this plugin serves. Two colours
did not follow the scheme adopted in the sibling plugin Stream-Mapparr on the
same day:

  Validate settings was green. It reads the settings, the collector state, the
  schedule row and the mail configuration, and writes nothing at all, which is
  the definition of blue. Stream-Mapparr's own Validate Settings is blue.

  Report an issue was grey, which is not a colour in the scheme. It points the
  operator outward to the issue tracker, which is cyan.

The scheme, and why each colour means one thing:

  red     can REMOVE something the user cares about, or take a channel off air
  orange  writes data or clears state, but removes nothing
  green   runs a normal operation that writes no user data
  cyan    sends something outward, to an inbox or an issue tracker
  blue    reads and reports, changing nothing

NOTHING HERE IS RED, and that is a fact about the plugin rather than an
oversight. It never writes to Dispatcharr at all: tests/test_no_mutations.py is
an abstract syntax tree guard that fails the build on any write shaped call to
the object relational mapper anywhere in the shipped modules. The only files it
can remove are its own old report copies, which is the same consequence
Stream-Mapparr colours orange for Clear CSV Exports.

Build report stays orange rather than becoming red now that it can delete old
report copies. The dated copies were already capped by count before any age
setting existed, so deleting them is not new, at least one always survives, and
colouring the plugin's primary and only data producing action red would empty
red of its meaning here.
"""
import io
import json
import os

import pytest
from conftest import find_invisible, find_non_ascii, load_plugin

load_plugin()

MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dustarr", "plugin.json")

EXPECTED_COLOURS = {
    # blue: reads and reports, changing nothing
    "validate_settings": "blue",
    "show_summary": "blue",
    # orange: writes data or clears state, but removes nothing the user needs
    "build_report": "orange",
    # cyan: points outward, to an issue tracker
    "report_issue": "cyan",
}

SCHEME_COLOURS = {"red", "orange", "green", "cyan", "blue"}


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module


def _actions(plugin):
    """The list Dispatcharr actually serves for an ENABLED plugin."""
    return plugin.Plugin.__new__(plugin.Plugin).actions


def _manifest():
    return json.load(open(MANIFEST, encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Every button is labelled and coloured
# --------------------------------------------------------------------------- #
def test_every_action_has_a_button_colour(plugin):
    missing = [a["id"] for a in _actions(plugin) if not a.get("button_color")]
    assert missing == [], f"actions with no button_color: {missing}"


def test_every_action_has_a_button_label(plugin):
    missing = [a["id"] for a in _actions(plugin) if not a.get("button_label")]
    assert missing == [], f"actions with no button_label: {missing}"


def test_this_plugin_declares_no_event_driven_actions(plugin):
    """An action invoked by a Dispatcharr event rather than pressed must carry
    neither a label nor a colour, because it is not a button. This plugin has
    none, so every action here is pressable and every one needs both."""
    handlers = [a["id"] for a in _actions(plugin) if a.get("events")]
    assert handlers == [], handlers


# --------------------------------------------------------------------------- #
# Colour means one thing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("action_id,colour", sorted(EXPECTED_COLOURS.items()))
def test_the_action_carries_the_colour_its_consequence_calls_for(
        plugin, action_id, colour):
    action = next((a for a in _actions(plugin) if a["id"] == action_id), None)
    assert action is not None, f"{action_id} is not served"
    assert action.get("button_color") == colour


def test_every_served_action_is_covered_by_the_colour_scheme(plugin):
    """A new action must be given a colour deliberately, not left to a default."""
    served = {a["id"] for a in _actions(plugin)}
    assert served == set(EXPECTED_COLOURS), served ^ set(EXPECTED_COLOURS)


def test_no_action_uses_a_colour_outside_the_scheme(plugin):
    """Report an issue was grey, which carries no meaning in this scheme."""
    strays = {a["id"]: a.get("button_color") for a in _actions(plugin)
              if a.get("button_color") not in SCHEME_COLOURS}
    assert strays == {}, strays


def test_nothing_is_red_because_this_plugin_cannot_remove_user_data(plugin):
    """Red is reserved for removing something the user cares about.

    This plugin never writes to Dispatcharr, so no action can qualify. If one
    ever does, this test should fail and be replaced by a confirmation dialog
    requirement, not simply deleted.
    """
    red = [a["id"] for a in _actions(plugin) if a.get("button_color") == "red"]
    assert red == [], red


def test_any_red_action_would_also_have_to_ask_for_confirmation(plugin):
    """Colour is the glance, the dialog is the guard. Holds vacuously today and
    starts guarding the moment a red action is added."""
    for a in _actions(plugin):
        if a.get("button_color") == "red":
            assert a.get("confirm"), f"{a['id']} is red but has no confirm dialog"


def test_only_the_action_that_writes_the_report_is_the_filled_button(plugin):
    """One primary action per form. The rest are outlines."""
    filled = [a["id"] for a in _actions(plugin)
              if a.get("button_variant") == "filled"]
    assert filled == ["build_report"], filled


# --------------------------------------------------------------------------- #
# The manifest cannot drift, because it declares nothing
# --------------------------------------------------------------------------- #
def test_the_manifest_declares_no_actions_so_it_cannot_drift(plugin):
    """plugin.py is the single source for the action list.

    In Stream-Mapparr the manifest and the served list had drifted across 22
    values, disagreeing in both directions on labels, colours and confirmation
    prompts, so reading either one alone gave a false picture of the interface.
    This plugin avoids that by declaring actions in exactly one place. If a
    list is ever added to the manifest, this test fails and must be replaced by
    one that compares the two, not deleted.
    """
    assert not _manifest().get("actions"), \
        "plugin.json has grown an actions list that can disagree with plugin.py"


# --------------------------------------------------------------------------- #
# Rules for text this plugin shows the operator
# --------------------------------------------------------------------------- #
def test_no_action_copy_uses_an_em_dash_or_a_double_hyphen(plugin):
    em_dash = chr(0x2014)
    en_dash = chr(0x2013)
    offenders = []
    for a in _actions(plugin):
        for key in ("label", "description", "button_label", "confirm"):
            text = a.get(key) or ""
            if not isinstance(text, str):
                continue
            if em_dash in text or en_dash in text or "--" in text:
                offenders.append(f"{a.get('id')}.{key}")
    assert offenders == [], offenders


def test_action_copy_carries_no_invisible_characters(plugin):
    """The emoji in a button label are deliberate. A zero width character, a
    soft hyphen or a replacement character is not, and a replacement character
    is the marker that a scripted edit mangled the text.
    """
    bad = {}
    for a in _actions(plugin):
        for key in ("label", "description", "button_label"):
            hits = find_invisible(a.get(key))
            if hits:
                bad[f"{a.get('id')}.{key}"] = hits
    assert bad == {}, bad


def test_every_action_description_is_plain_ascii(plugin):
    """The button LABEL carries an emoji on purpose. The description is prose
    that also reaches logs and files, so it stays plain."""
    bad = {}
    for a in _actions(plugin):
        outside = find_non_ascii(a.get("description"))
        if outside:
            bad[a.get("id")] = outside
    assert bad == {}, bad
