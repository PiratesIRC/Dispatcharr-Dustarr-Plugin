import json
from pathlib import Path

from conftest import PLUGIN_DIR, load_plugin

VALID_FIELD_TYPES = {"string", "number", "boolean", "select", "text", "info"}


def test_init_exports_only_plugin():
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "Plugin" in src
    mod = {}
    exec(compile(src.replace("from .plugin import Plugin",
                             "Plugin = object"), "__init__", "exec"), mod)
    exported = [k for k in mod if not k.startswith("__")]
    assert exported == ["Plugin"]


def test_plugin_json_matches_plugin_version():
    plugin = load_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin.PLUGIN_VERSION


def test_fields_use_only_supported_types_and_have_defaults():
    plugin = load_plugin()
    for field in plugin.FIELDS:
        assert field["type"] in VALID_FIELD_TYPES, field["id"]
        if field["type"] != "info":
            assert "default" in field, field["id"]
        if field["type"] == "select":
            # Dispatcharr's PluginFieldOptionSerializer declares value/label as
            # CharField -- an int value is silently dropped from the UI.
            for opt in field["options"]:
                assert isinstance(opt["value"], str), field["id"]


def test_quick_start_is_the_very_first_field():
    """Operator decision 2026-07-26: Quick Start sits at the VERY top, above
    the read-only box, which stays as the second field. Ordering is the whole
    point of an orientation panel, so pin it."""
    plugin = load_plugin()
    assert plugin.FIELDS[0]["id"] == "_section_quickstart"
    assert plugin.FIELDS[0]["type"] == "info"
    assert plugin.FIELDS[0]["label"] == "Quick Start"
    # The read-only box is NOT folded in or dropped.
    assert plugin.FIELDS[1]["id"] == "info"


def test_quick_start_names_every_action_by_its_real_button_label():
    """A quick start that names a button the UI does not have is worse than
    none. Bind the copy to the ACTIONS list rather than to a hardcoded string."""
    plugin = load_plugin()
    body = plugin.FIELDS[0]["description"]
    for action in plugin.ACTIONS:
        assert action["label"] in body, action["label"]


def test_quick_start_uses_no_em_dashes_and_is_one_paragraph():
    """Operator instruction: no em dashes anywhere in the copy. And the info
    body renders as one flowing paragraph (EPG-Janitor's reference
    implementation does not risk multi-line layout), so no embedded newlines."""
    plugin = load_plugin()
    body = plugin.FIELDS[0]["description"]
    assert "—" not in body, "em dash in Quick Start copy"
    assert "–" not in body, "en dash in Quick Start copy"
    assert "\n" not in body, "Quick Start must be a single paragraph"


def test_quick_start_warns_about_the_30_day_warmup():
    """The 'not trustworthy' banner is the single most likely thing to be
    mistaken for a bug, so orientation copy must pre-empt it."""
    plugin = load_plugin()
    body = plugin.FIELDS[0]["description"].lower()
    assert "not trustworthy" in body
    assert "30 days" in body


def test_plugin_instantiates_without_side_effects():
    plugin = load_plugin()
    inst = plugin.Plugin()
    assert inst.name and inst.version == plugin.PLUGIN_VERSION
    assert isinstance(inst.fields, list) and isinstance(inst.actions, list)


def test_webhook_surface_is_gone_and_notify_toggle_present():
    plugin = load_plugin()
    field_ids = [f["id"] for f in plugin.FIELDS]
    assert "notify_enabled" in field_ids
    assert "webhook_url" not in field_ids
    assert "webhook_format" not in field_ids
    assert "report_base_url" in field_ids          # kept: the notify url uses it
    action_ids = [a["id"] for a in plugin.ACTIONS]
    assert "send_webhook_now" not in action_ids
    nf = next(f for f in plugin.FIELDS if f["id"] == "notify_enabled")
    assert nf["type"] == "boolean" and nf["default"] is False


# -- ACTION contract (dustarr had NONE -- only a subset check) -------------
# A field or action that fails Dispatcharr's serializer is SILENTLY DROPPED
# (logger.warning only, and the plugin still loads) -- how smtp_security shipped
# declared-but-never-rendered in Newsflasharr. The only assertion dustarr had
# was a SUBSET check, so a dropped, renamed or misspelt action passed.

VALID_ACTION_KEYS = {"id", "label", "description", "confirm", "button_label",
                     "button_variant", "button_color", "events"}
ACTION_IDS = {"build_report", "email_report_now", "show_summary",
              "validate_settings", "report_issue"}


def test_action_ids_match_spec():
    plugin = load_plugin()
    assert {a["id"] for a in plugin.ACTIONS} == ACTION_IDS


def test_no_action_carries_a_key_dispatcharr_would_reject():
    plugin = load_plugin()
    for action in plugin.ACTIONS:
        assert set(action) <= VALID_ACTION_KEYS, action["id"]


def test_every_action_carries_a_button_label():
    plugin = load_plugin()
    for action in plugin.ACTIONS:
        assert action.get("button_label"), action["id"]


def test_action_text_fields_are_non_empty_strings():
    plugin = load_plugin()
    for action in plugin.ACTIONS:
        for key in ("id", "label", "description"):
            assert isinstance(action.get(key), str) and action[key].strip(), \
                f"{action.get('id')}.{key}"


def test_task_path_matches_the_real_package_directory():
    """Dispatcharr derives the import path from the package DIRECTORY name.

    A mismatch fails invisibly: Beat's total_run_count counts messages sent,
    not executed, so a row dispatching a nonexistent task looks healthy
    forever (bug-075). Nothing else in the suite pins this literal.
    """
    plugin = load_plugin()
    expected = f"_dispatcharr_plugin_{PLUGIN_DIR.name}.plugin.build_report_task"
    assert plugin.TASK_PATH == expected


def test_validate_settings_is_the_first_action():
    """Operator decision 2026-07-27. It is the button to press before any
    other, and it writes nothing."""
    plugin = load_plugin()
    assert plugin.ACTIONS[0]["id"] == "validate_settings"


def test_report_issue_action_carries_the_real_repository_url():
    plugin = load_plugin()
    assert plugin.ISSUES_URL.startswith("https://github.com/")
    assert plugin.ISSUES_URL.endswith("/issues")
    assert any(a["id"] == "report_issue" for a in plugin.ACTIONS)


def test_email_action_states_its_newsflasharr_requirement():
    """If the checks were ever removed, the description is the fallback the
    operator reads. It must name what is required either way."""
    plugin = load_plugin()
    desc = next(a for a in plugin.ACTIONS
                if a["id"] == "email_report_now")["description"].lower()
    assert "newsflasharr" in desc
    assert "smtp" in desc
