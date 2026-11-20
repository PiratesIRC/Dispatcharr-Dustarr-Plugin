"""The Dispatcharr 0.34 settings form built by Plugin.get_fields.

0.33 and earlier read the flat `Plugin.fields`; 0.34 calls `get_fields` and gets
collapsible sections plus Excluded groups as a pick list under a new id. These
tests bind the two forms together, the write-back of the pick list into the old
text setting, and the fallback to the flat form on any error.
"""
import copy
import json
import sys

import pytest
from conftest import PLUGIN_DIR, load_plugin

load_plugin()
ff = sys.modules["dustarr_under_test.form_fields"]
gateway = sys.modules["dustarr_under_test.gateway"]

OLD_TYPES = {"string", "number", "boolean", "select", "text", "info"}
NEW_ONLY_ATTRS = {"section", "collapsed", "columns", "accept", "max_size", "rows"}
GROUPS = ["Movies", "US: News", "US: Sports", "UK: General"]


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module


def _form(plugin, settings=None, groups=GROUPS):
    return ff.build_form(plugin.FIELDS, settings or {}, groups)


def _by_id(fields):
    return {f["id"]: f for f in fields}


def test_flat_form_uses_only_types_dispatcharr_033_knows(plugin):
    for field in plugin.FIELDS:
        assert field["type"] in OLD_TYPES, field["id"]
        assert not NEW_ONLY_ATTRS & set(field), field["id"]


def test_building_the_form_does_not_modify_the_flat_form(plugin):
    before = copy.deepcopy(plugin.FIELDS)
    _form(plugin, {"exclude_groups": "US: News"})
    assert plugin.FIELDS == before


def test_every_flat_id_is_in_the_034_form_or_mapped_to_a_new_id(plugin):
    form = _by_id(_form(plugin))
    for field in plugin.FIELDS:
        fid = field["id"]
        assert fid in form or ff.NEW_ID_BY_OLD_ID.get(fid) in form, fid


def test_every_section_header_becomes_a_section_with_its_text_kept(plugin):
    form = _by_id(_form(plugin))
    headers = [f for f in plugin.FIELDS if ff.is_section_header(f)]
    assert len(headers) == 6
    for header in headers:
        marker = form[header["id"]]
        assert marker["type"] == "section"
        assert marker["label"] == header["label"]
        about = form[header["id"] + ff.ABOUT_SUFFIX]
        assert about["type"] == "info"
        assert about["description"] == header["description"]
        assert about["section"] == header["id"]


def test_every_non_section_field_belongs_to_a_declared_section(plugin):
    form = _form(plugin)
    declared = {f["id"] for f in form if f["type"] == "section"}
    for field in form:
        if field["type"] != "section":
            assert field.get("section") in declared, field["id"]


def test_collapsed_sections(plugin):
    form = _by_id(_form(plugin))
    collapsed = {f["id"] for f in form.values()
                 if f["type"] == "section" and f["collapsed"]}
    assert collapsed == {"_section_collection", "_section_usage"}


def test_exclude_groups_becomes_a_pick_list_under_a_new_id(plugin):
    form = _by_id(_form(plugin))
    assert "exclude_groups" not in form
    field = form["exclude_group_list"]
    assert field["type"] == "multiselect"
    assert field["section"] == "_section_exclusions"
    assert "refresh control" in field["help_text"]


def test_pick_list_default_comes_from_the_saved_text_in_database_spelling(plugin):
    field = _by_id(_form(plugin, {"exclude_groups": "us: news, Movies"}))["exclude_group_list"]
    assert field["default"] == ["US: News", "Movies"]


def test_pick_list_default_falls_back_to_the_field_default(plugin):
    field = _by_id(_form(plugin))["exclude_group_list"]
    assert field["default"] == ff.split_list(gateway.DEFAULT_EXCLUDE_GROUPS)


def test_every_default_and_stored_value_is_an_option(plugin):
    """Dispatcharr rejects a save holding a value that is not an option, so a
    group no longer in the database must stay in the list."""
    settings = {"exclude_groups": "Gone Group, US: News",
                "exclude_group_list": ["Also Gone"]}
    field = _by_id(_form(plugin, settings))["exclude_group_list"]
    values = {o["value"] for o in field["options"]}
    assert {"Gone Group", "US: News", "Also Gone", "Movies"} <= values
    assert set(field["default"]) <= values


def test_a_group_name_holding_a_comma_is_not_offered(plugin):
    field = _by_id(_form(plugin, groups=GROUPS + ["A, B"]))["exclude_group_list"]
    assert "A, B" not in {o["value"] for o in field["options"]}


def test_options_are_deduplicated_ignoring_case(plugin):
    field = _by_id(_form(plugin, {"exclude_groups": "movies"}))["exclude_group_list"]
    values = [o["value"] for o in field["options"]]
    assert values.count("Movies") == 1
    assert "movies" not in values


def test_long_help_is_split_into_titled_info_fields(plugin):
    form = _form(plugin)
    ids = [f["id"] for f in form]
    flat = _by_id(plugin.FIELDS)
    for fid, (short, paragraphs) in ff.HELP_PARAGRAPHS.items():
        field = _by_id(form)[fid]
        assert field.get("help_text", field.get("description")) == short
        assert len(short) < len(flat[fid]["description"])
        position = ids.index(fid)
        for number, (title, text) in enumerate(paragraphs, start=1):
            info = form[position + number]
            assert info["id"] == f"{fid}{ff.HELP_SUFFIX}{number}"
            assert info["label"] == title and info["description"] == text
            assert info["section"] == field["section"]


def test_rendered_copy_has_no_dashes_line_breaks_or_contractions(plugin):
    texts = [meta["help_text"] for meta in ff.NEW_FIELDS.values()]
    for short, paragraphs in ff.HELP_PARAGRAPHS.values():
        texts.append(short)
        texts.extend(title + " " + text for title, text in paragraphs)
    for text in texts:
        assert "\n" not in text
        assert chr(0x2014) not in text and chr(0x2013) not in text and "--" not in text
        assert all(ord(c) < 128 for c in text)
        for word in ("n't", "'s ", "'re", "'ll", "'ve"):
            assert word not in text, (word, text)


def test_legacy_settings_writes_the_list_back_into_the_text_setting():
    out = ff.legacy_settings({"exclude_group_list": ["US: News", " Movies "],
                              "exclude_groups": "old"}, True)
    assert out["exclude_groups"] == "US: News, Movies"


def test_legacy_settings_round_trips_through_the_form(plugin):
    field = _by_id(_form(plugin, {"exclude_groups": "US: News, Movies"}))["exclude_group_list"]
    out = ff.legacy_settings({"exclude_group_list": field["default"]}, True)
    parsed = gateway.parse_exclusions(out)["groups"]
    assert parsed == {"us: news", "movies"}


def test_an_empty_pick_list_clears_the_exclusions():
    out = ff.legacy_settings({"exclude_group_list": [], "exclude_groups": "US: News"}, True)
    assert out["exclude_groups"] == ""
    assert gateway.parse_exclusions(out)["groups"] == set()


def test_legacy_settings_does_nothing_on_033():
    """On 0.33 (no field_versions module) a list left over from a downgrade
    must not override the text the operator now edits."""
    settings = {"exclude_group_list": ["Movies"], "exclude_groups": "US: News"}
    assert ff.legacy_settings(settings, False)["exclude_groups"] == "US: News"


def test_legacy_settings_ignores_a_non_list_value():
    out = ff.legacy_settings({"exclude_group_list": "Movies", "exclude_groups": "x"}, True)
    assert out["exclude_groups"] == "x"


def test_fields_api_available_is_false_without_field_versions(monkeypatch):
    monkeypatch.setitem(sys.modules, "apps.plugins.field_versions", None)
    assert gateway.fields_api_available() is False


def test_fields_api_available_is_true_with_field_versions(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "apps.plugins.field_versions",
                        types.ModuleType("apps.plugins.field_versions"))
    assert gateway.fields_api_available() is True


def test_get_fields_builds_the_sectioned_form(plugin, monkeypatch):
    monkeypatch.setattr(plugin.gateway, "channel_group_names", lambda: GROUPS)
    fields = plugin.Plugin().get_fields({"settings": {"exclude_groups": "Movies"}})
    form = _by_id(fields)
    assert form["_section_exclusions"]["type"] == "section"
    assert form["exclude_group_list"]["default"] == ["Movies"]


def test_get_fields_falls_back_to_the_flat_form_on_an_exception(plugin, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(plugin.form_fields, "build_form", boom)
    fields = plugin.Plugin().get_fields({"settings": {}})
    assert fields == list(plugin.FIELDS)


def test_get_fields_accepts_no_context(plugin, monkeypatch):
    monkeypatch.setattr(plugin.gateway, "channel_group_names", lambda: [])
    assert any(f["type"] == "section" for f in plugin.Plugin().get_fields())


def test_run_reads_the_pick_list_value(plugin, monkeypatch):
    """An action receives the 0.34 list and the code reads the text form."""
    monkeypatch.setattr(plugin.gateway, "fields_api_available", lambda: True)
    seen = {}

    def fake_validate(self, settings):
        seen.update(settings)
        return {"status": "ok", "message": "ok"}

    monkeypatch.setattr(plugin.Plugin, "_validate", fake_validate)
    monkeypatch.setattr(plugin, "ensure_collector", lambda *_a, **_k: None)
    monkeypatch.setattr(plugin, "sync_schedule", lambda *_a, **_k: None)
    monkeypatch.setattr(plugin, "_report_usage", lambda *_a, **_k: None)
    plugin.Plugin.__new__(plugin.Plugin).run(
        "validate_settings", context={"settings": {"exclude_group_list": ["Movies"]}})
    assert seen["exclude_groups"] == "Movies"


def test_scheduled_settings_load_reads_the_pick_list_value(plugin, monkeypatch):
    monkeypatch.setattr(plugin.gateway, "fields_api_available", lambda: True)
    models = sys.modules["apps.plugins.models"]

    class Row:
        settings = {"exclude_group_list": ["Movies", "US: News"]}

    class Manager:
        def get(self, **_k):
            return Row()

    monkeypatch.setattr(models.PluginConfig, "objects", Manager(), raising=False)
    assert plugin._load_settings()["exclude_groups"] == "Movies, US: News"


def test_manifest_declares_version_1_and_the_capabilities():
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["capabilities"] == ["outbound_network", "filesystem_write",
                                        "proxy_internals"]
