"""The user guide documents every setting and every button, by the name the interface uses.

Checked against the lists the plugin declares rather than by eye, so a setting
that exists but is undocumented shows up, and a setting renamed in the form
cannot leave a stale name behind in the guide. Measured 2026-09-05 against
docs/USER-GUIDE.md: three settings failed, one of them because this same pass
renamed it.

The README deliberately does NOT enumerate settings. It describes what the
plugin is for and points at the user guide, so it is checked only for the
things it does claim.
"""
import io
import os

import pytest
from conftest import load_plugin

load_plugin()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(REPO, "docs", "USER-GUIDE.md")
README = os.path.join(REPO, "README.md")


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _settings(plugin):
    return [f for f in plugin.Plugin.__new__(plugin.Plugin).fields
            if f.get("type") != "info"]


def test_the_user_guide_names_every_setting_the_form_shows(plugin):
    """A setting the operator can see and cannot look up is an undocumented one."""
    guide = _read(GUIDE)
    missing = [f["id"] for f in _settings(plugin) if f["label"] not in guide]
    assert missing == [], f"settings absent from the user guide: {missing}"


def test_the_user_guide_names_every_button(plugin):
    guide = _read(GUIDE)
    actions = plugin.Plugin.__new__(plugin.Plugin).actions
    missing = [a["id"] for a in actions if a["label"] not in guide]
    assert missing == [], f"buttons absent from the user guide: {missing}"


def test_the_user_guide_carries_no_stale_setting_name(plugin):
    """Names this plugin used to show and no longer does. A guide that still
    carries one sends the reader looking for a control that is not there."""
    guide = _read(GUIDE)
    retired = ["Top/bottom N"]
    stale = [name for name in retired if name in guide]
    assert stale == [], f"the guide still uses a retired setting name: {stale}"


def test_the_readme_points_at_the_user_guide_rather_than_listing_settings(plugin):
    """The README is deliberately not a settings reference. This pins that
    choice, so a future reader does not mistake the absence for a gap."""
    readme = _read(README)
    assert "USER-GUIDE" in readme or "User Guide" in readme


def test_neither_document_uses_an_em_dash_or_a_double_hyphen():
    """Standing operator instruction for committed, user-facing text."""
    for path in (GUIDE, README):
        # A Markdown table separator row is structure, not prose, so its
        # hyphens are not em dash substitutes.
        prose = [line for line in _read(path).splitlines()
                 if not line.replace("|", "").replace("-", "").replace(":", "").strip() == ""]
        text = chr(10).join(prose)
        assert chr(0x2014) not in text, path
        assert chr(0x2013) not in text, path
        assert "--" not in text, path


# --------------------------------------------------------------------------- #
# Each setting is documented under the SAME heading the form shows it under
# --------------------------------------------------------------------------- #
# Found by review on 2026-09-05. The settings-form sections were renamed and the
# guide's headings were renamed to match, but the row for the row-count setting
# was left inside the "When a channel counts as unused" table while its own text
# said it "now sits with the output settings". A reader following the guide
# looked in the wrong section of the plugin card. The earlier test only checked
# that each label appeared SOMEWHERE in the document, so nothing caught it.


def _guide_section_of(label):
    """The nearest '### ' heading above the line that documents `label`."""
    heading = None
    for line in _read(GUIDE).splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
        if line.lstrip().startswith(f"| **{label}**"):
            return heading
    return None


def _form_section_of(plugin, field_id):
    """The nearest '_section_' heading above `field_id` in the served form."""
    heading = None
    for f in plugin.Plugin.__new__(plugin.Plugin).fields:
        fid = str(f.get("id", ""))
        if fid.startswith("_section_"):
            heading = f.get("label")
        if fid == field_id:
            return heading
    return None


def test_every_setting_is_documented_under_the_heading_the_form_shows_it_under(plugin):
    """Checked for every setting, not one: with a single setting this passes
    while the guide misfiles all the others."""
    wrong = {}
    for f in _settings(plugin):
        guide_section = _guide_section_of(f["label"])
        form_section = _form_section_of(plugin, f["id"])
        if guide_section != form_section:
            wrong[f["id"]] = {"guide says": guide_section, "form says": form_section}
    assert wrong == {}, f"documented under the wrong heading: {wrong}"


def test_the_guide_headings_match_the_form_headings(plugin):
    """If the two drift apart the test above cannot tell a misfiled setting
    from a renamed heading, so the headings are pinned separately."""
    form = [f.get("label") for f in plugin.Plugin.__new__(plugin.Plugin).fields
            if str(f.get("id", "")).startswith("_section_")
            and f.get("id") != "_section_quickstart"]
    guide = [line[4:].strip() for line in _read(GUIDE).splitlines()
             if line.startswith("### ")]
    missing = [h for h in form if h not in guide]
    assert missing == [], f"form headings with no matching guide heading: {missing}"
