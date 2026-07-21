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
