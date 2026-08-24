import json
from pathlib import Path

import pytest
from conftest import PLUGIN_DIR, load_plugin

VALID_FIELD_TYPES = {"string", "number", "boolean", "select", "text", "info"}


@pytest.fixture
def lp():
    """The loaded plugin module, with a fresh Plugin() attached as `.instance`.

    `lp.instance.fields` is the served field list (Plugin.__init__ builds it),
    which is what must be asserted on: the manifest declares none, so reading
    it there gives a false pass. Module-level helpers such as
    `coerce_settings` and `_thresholds_fingerprint` are reachable straight off
    `lp` because `lp` is the plugin module itself.
    """
    plugin = load_plugin()
    plugin.instance = plugin.Plugin()
    return plugin


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
    # Removed with the HTTP hosting: there is no report URL to build a base
    # for, and a leftover field would invite someone to serve the report again.
    assert "report_base_url" not in field_ids
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
# `email_report_now` merged into `build_report`: one job, one button. The
# notify_enabled setting already says whether this box emails its reports.
ACTION_IDS = {"build_report", "show_summary",
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


def test_build_report_action_states_its_newsflasharr_requirement():
    """If the checks were ever removed, the description is the fallback the
    operator reads. It must name what is required either way. This moved from
    the separate `email_report_now` action when the two buttons were merged."""
    plugin = load_plugin()
    desc = next(a for a in plugin.ACTIONS
                if a["id"] == "build_report")["description"].lower()
    assert "newsflasharr" in desc
    assert "smtp" in desc


def test_recent_window_days_is_declared_with_a_default_of_30(lp):
    field = next(f for f in lp.instance.fields if f["id"] == "recent_window_days")
    assert field["type"] == "number"
    assert field["default"] == 30


def test_recent_window_days_is_clamped_and_cast_to_int(lp):
    assert lp.coerce_settings({"recent_window_days": -5})["recent_window_days"] == 7
    assert lp.coerce_settings({"recent_window_days": 99999})["recent_window_days"] == 3650
    value = lp.coerce_settings({"recent_window_days": 45.7})["recent_window_days"]
    assert value == 45
    assert isinstance(value, int)


def test_recent_window_days_floor_is_7_not_1(lp):
    """A window shorter than 7 days lets an always-on channel go cold in a
    single day: its last completed watch and its last tune both predate a
    1 day window well before the stream itself has stopped, so it would be
    listed as abandoned while it is actually on screen. See the comment on
    _NUMERIC_FLOORS in plugin.py."""
    assert lp.coerce_settings({"recent_window_days": 1})["recent_window_days"] == 7
    assert lp.coerce_settings({"recent_window_days": 6})["recent_window_days"] == 7
    assert lp.coerce_settings({"recent_window_days": 7})["recent_window_days"] == 7


def _changed_value(lp, field):
    """A value that coerce_settings will treat as genuinely different from the
    field's own default, respecting each type's own rules (a select must stay
    one of its declared options, a number must stay in its floor/ceiling)."""
    fid = field["id"]
    default = field.get("default")
    if field["type"] == "boolean":
        return not default
    if field["type"] == "select":
        options = [opt["value"] for opt in field.get("options", [])]
        for opt in options:
            if opt != default:
                return opt
        raise AssertionError(f"select field {fid!r} has no alternate option")
    if field["type"] == "number":
        low, high = lp._NUMERIC_FLOORS.get(fid, (None, None))
        candidate = float(default) + 1.0
        if high is not None:
            candidate = min(candidate, high)
        if low is not None:
            candidate = max(candidate, low)
        assert candidate != default, f"could not derive a changed value for {fid!r}"
        return candidate
    # string / text: any different string coerces through unchanged.
    return f"{default}-changed"


def test_report_only_settings_do_not_respawn_the_collector(lp):
    """Every setting the collector does not read must leave the fingerprint
    unchanged: a respawn builds a fresh Sessionizer and forfeits every
    in-flight watch session, so a setting that only affects the report (the
    top/bottom count, the unused threshold, the alarm ceiling, the
    notification toggle, the report schedule, the exclusion settings, and so
    on) must never trigger one.

    Driven from the served field list, not a hand-written name list, so a
    report-only setting added later is covered automatically. This test would
    fail if `_thresholds_fingerprint` reverted to hashing every setting: the
    non-collection fields it drives through here (e.g. `top_n`,
    `never_watched_ceiling`, `notify_enabled`, `report_schedule`) would then
    change the fingerprint just like a collection threshold does.
    """
    base = lp.coerce_settings({})
    base_fp = lp._thresholds_fingerprint(base)
    collection_keys = set(lp.sessionizer.DEFAULTS)
    for field in lp.instance.fields:
        if field["type"] == "info" or field["id"] in collection_keys:
            continue
        changed = lp.coerce_settings({field["id"]: _changed_value(lp, field)})
        assert changed[field["id"]] != base[field["id"]], (
            f"{field['id']!r} did not actually change value in coerce_settings")
        assert lp._thresholds_fingerprint(changed) == base_fp, (
            f"report-only setting {field['id']!r} unexpectedly respawned the collector")


def test_every_collection_threshold_changes_the_hash(lp):
    """Every setting the collector DOES read must change the fingerprint on a
    real change. Driven from sessionizer.DEFAULTS, the same dictionary
    `_thresholds_fingerprint` derives its key set from, so the test and the
    implementation cannot drift apart."""
    base = lp.coerce_settings({})
    base_fp = lp._thresholds_fingerprint(base)
    fields_by_id = {field["id"]: field for field in lp.instance.fields}
    for key in lp.sessionizer.DEFAULTS:
        field = fields_by_id[key]
        changed = lp.coerce_settings({key: _changed_value(lp, field)})
        assert changed[key] != base[key], (
            f"{key!r} did not actually change value in coerce_settings")
        assert lp._thresholds_fingerprint(changed) != base_fp, (
            f"collection threshold {key!r} did not respawn the collector")


def test_fingerprint_is_stable_when_nothing_changes(lp):
    """Calling coerce_settings twice on the same input must fingerprint the
    same, or ensure_collector would respawn the collector every single call
    for no reason at all."""
    settings = {"poll_interval_s": 15, "top_n": 20}
    first = lp._thresholds_fingerprint(lp.coerce_settings(settings))
    second = lp._thresholds_fingerprint(lp.coerce_settings(dict(settings)))
    assert first == second


def test_manifest_declares_the_hub_required_metadata():
    """The Dispatcharr Plugin Hub manifest schema REQUIRES a `license` field,
    so a listing is refused without one. `repo_url` and `help_url` are what the
    plugin card links out to. Sibling plugins carry the same four keys, and
    this test is what stops one being dropped by a future manifest edit.

    `discord_thread` is deliberately NOT asserted: no support thread exists for
    this plugin yet, and an invented thread id would render a dead link on the
    plugin card.
    """
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["license"] == "MIT"
    for key in ("repo_url", "help_url"):
        assert manifest[key].startswith("https://github.com/"), key


def test_manifest_repo_url_agrees_with_the_url_the_report_renders():
    """`reports.py` cannot import `plugin.py`, so it duplicates REPO_URL. The
    manifest is a THIRD copy. Bind all of them together here: three URLs that
    can drift independently is how a rename ships a dead link.
    """
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    reports_src = (PLUGIN_DIR / "reports.py").read_text(encoding="utf-8")
    assert f'REPO_URL = "{manifest["repo_url"]}"' in reports_src
    assert f'ISSUES_URL = "{manifest["repo_url"]}/issues"' in reports_src
