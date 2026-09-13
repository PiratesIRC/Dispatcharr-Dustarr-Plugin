"""The badge refresh script must publish only a whole number, and never invent one.

scripts/update_reports_built_badge.py reads /data/dustarr/report_count.json from
the container and writes a Shields.io endpoint document to a Gist. The number
becomes public, so two things are pinned here: the parse degrades to 0 on
anything it cannot trust (a badge must not invent activity, the same direction
as plugin.read_report_count), and the endpoint document carries nothing but the
label, the formatted total and a colour.
"""
import importlib.util
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "update_reports_built_badge.py"


def _load():
    spec = importlib.util.spec_from_file_location("update_reports_built_badge", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_good_count_file_parses_to_its_number():
    assert _load().parse_count('{"reports_built": 8}') == 8


def test_zero_reports_is_zero_not_an_error():
    assert _load().parse_count('{"reports_built": 0}') == 0


def test_a_missing_or_empty_file_degrades_to_zero():
    mod = _load()
    assert mod.parse_count("") == 0
    assert mod.parse_count(None) == 0


def test_corrupt_json_degrades_to_zero():
    assert _load().parse_count('{"reports_built": ') == 0


def test_a_missing_key_degrades_to_zero():
    assert _load().parse_count('{"other": 5}') == 0


def test_a_negative_or_non_numeric_value_degrades_to_zero():
    mod = _load()
    assert mod.parse_count('{"reports_built": -3}') == 0
    assert mod.parse_count('{"reports_built": "many"}') == 0
    assert mod.parse_count('{"reports_built": null}') == 0


def test_a_json_list_degrades_to_zero():
    assert _load().parse_count('[1, 2, 3]') == 0


def test_endpoint_document_carries_only_the_public_fields():
    doc = _load().endpoint_document(1234)
    assert set(doc) == {"schemaVersion", "label", "message", "color"}
    assert doc["schemaVersion"] == 1
    assert doc["label"] == "Reports Built"
    assert doc["message"] == "1,234"
    json.dumps(doc)


def test_the_gist_id_file_is_committed_next_to_the_script():
    """A re-clone must update the same Gist rather than silently create a second."""
    id_file = REPO / "scripts" / ".reports_built_badge_gist"
    assert id_file.is_file(), "scripts/.reports_built_badge_gist is missing"
    assert id_file.read_text(encoding="utf-8").strip(), "the gist id file is empty"


def test_the_readme_badge_points_at_the_recorded_gist():
    """Renaming the Gist file or changing the id breaks the badge silently."""
    mod = _load()
    gist_id = (REPO / "scripts" / ".reports_built_badge_gist").read_text(encoding="utf-8").strip()
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    expected = (f"https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/"
                f"PiratesIRC/{gist_id}/raw/{mod.GIST_FILENAME}")
    assert expected in readme, "README.md does not carry the Reports Built badge for the recorded gist"
