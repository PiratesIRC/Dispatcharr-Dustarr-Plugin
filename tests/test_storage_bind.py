"""data_paths.bind rebinds the plugin's state constants to the data directory, once per import.

These tests drive data_paths.bind with a fake layout (the vendored resolve is replaced) and with
the legacy literals read from the source, so they never touch a real /data path.
"""
import ast
import os
import sys
import types

import pytest
from storage_helpers import PLUGIN_SOURCE, STATE_NAMES, Log, make_cfg, make_layout, source_literals, tmp_legacy


@pytest.fixture
def data_paths(plugin_impl):
    return plugin_impl.data_paths


def test_bind_rebinds_every_state_row_and_records_the_legacy_paths(data_paths, monkeypatch, tmp_path):
    legacy = tmp_legacy(tmp_path)
    cfg = make_cfg(dict(legacy, DATA_DIR="/data/dustarr"))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)

    got = data_paths.bind(cfg, str(tmp_path / "plugins" / "dustarr" / "plugin.py"), Log())

    assert got is layout
    assert cfg.LEGACY_PATHS == legacy
    assert cfg.DATA_DIR == layout.data_dir
    assert cfg.USAGE_FILE_PATH == os.path.join(layout.data_dir, "usage.json")
    assert cfg.SCHEDULED_RUN_PATH == os.path.join(layout.data_dir, "scheduled_run.json")
    assert cfg.NOTIFY_STATE_PATH == os.path.join(layout.data_dir, "notify_state.json")
    assert cfg.USAGE_DATA_DIR == os.path.join(layout.data_dir, "usage")
    # The report fallback is the shared Newsflasharr folder, not the data directory.
    assert cfg.REPORT_FALLBACK_DIR == os.path.join(layout.plugins_dir, "_shared", "newsflasharr", "dustarr")
    assert cfg.STORAGE_MIGRATION is not None


def test_the_report_counter_path_is_not_moved_by_bind(data_paths, monkeypatch, tmp_path):
    """Newsflasharr reads /data/<source>/report_count.json, so the counter keeps its literal
    path after a bind into the data directory, and it is no carry-over row."""
    literals = source_literals()
    cfg = make_cfg(dict(literals))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)

    data_paths.bind(cfg, str(tmp_path / "plugin.py"), Log())

    assert cfg.REPORT_COUNT_PATH == "/data/dustarr/report_count.json"
    assert "REPORT_COUNT_PATH" not in cfg.LEGACY_PATHS
    assert all(row[0] != "REPORT_COUNT_PATH" for row in data_paths.SPEC.table)


def test_bind_names_the_legacy_paths_the_way_the_source_does(data_paths):
    """The table row names must be module-level constants the source defines, each a /data path."""
    literals = source_literals()
    for name in STATE_NAMES:
        assert name in literals, f"{name} is not a plugin.py constant"
        assert literals[name].startswith("/data/")


def test_legacy_mode_keeps_the_old_paths_exactly(data_paths, monkeypatch, tmp_path):
    literals = source_literals()
    cfg = make_cfg(dict(literals))
    monkeypatch.setattr(
        data_paths._ps, "resolve",
        lambda spec, plugin_dir, key, logger: data_paths.Layout(True, None, None, key, "test"))

    layout = data_paths.bind(cfg, str(tmp_path / "plugin.py"), Log())

    assert layout.legacy is True
    for name, value in literals.items():
        assert getattr(cfg, name) == value, name
    assert cfg.STORAGE_MIGRATION is None


def test_a_failed_carry_over_falls_back_to_the_legacy_paths(data_paths, monkeypatch, tmp_path):
    literals = source_literals()
    cfg = make_cfg(dict(literals))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(data_paths._ps, "migrate", boom)
    log = Log()
    result = data_paths.bind(cfg, str(tmp_path / "plugin.py"), log)

    assert result.legacy is True
    assert cfg.DATA_DIR == literals["DATA_DIR"], "DATA_DIR must stay legacy when the bind fails"
    for name in STATE_NAMES:
        assert getattr(cfg, name) == literals[name], name
    assert log.errors, "a failed carry-over must be logged"


def test_the_session_plugin_runs_in_legacy_mode(plugin_impl):
    """No Dispatcharr sandbox in the test process, so the real module never resolves a directory."""
    assert plugin_impl.STORAGE_LAYOUT.legacy is True
    # conftest moves DATA_DIR under tmp_path; the production literal is checked in source.
    assert source_literals()["DATA_DIR"] == "/data/dustarr"


def test_the_usage_reporter_is_built_after_bind_on_the_rebound_directory(plugin_impl):
    """The usage reporter is built at import, so it needs the rebound folder and must come after
    data_paths.bind in the source (a folder captured before bind would stay legacy)."""
    tree = ast.parse(PLUGIN_SOURCE.read_text(encoding="utf-8"))
    bind_line = usage_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "bind" \
                and getattr(node.func.value, "id", "") == "data_paths":
            bind_line = node.lineno
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "USAGE" for t in node.targets):
            usage_line = node.lineno
            keywords = {k.arg: k.value for k in node.value.keywords}
            assert ast.unparse(keywords["data_dir"]) == "USAGE_DATA_DIR"
    assert bind_line is not None and usage_line is not None
    assert bind_line < usage_line


def test_install_id_blocked_disables_reporting_for_this_process(plugin_impl):
    usage = types.SimpleNamespace(report=lambda *a, **k: "sent")
    plugin_impl._apply_usage_storage(usage, types.SimpleNamespace(install_id_blocked=True))
    assert usage.report() is None, "a blocked carry-over must not let this process mint a second id"


def test_install_id_not_blocked_keeps_reporting(plugin_impl):
    usage = types.SimpleNamespace(report=lambda *a, **k: "sent")
    plugin_impl._apply_usage_storage(usage, types.SimpleNamespace(install_id_blocked=False))
    assert usage.report() == "sent"
    plugin_impl._apply_usage_storage(usage, None)
    assert usage.report() == "sent"


def test_a_failed_install_id_copy_sets_the_blocked_flag(data_paths, monkeypatch, tmp_path):
    """The real migrate: an old install id that cannot be copied blocks reporting, and the
    carry-over is not marked settled, so the next process retries it."""
    legacy = tmp_legacy(tmp_path)
    usage_old = os.path.join(legacy["USAGE_DATA_DIR"], "dustarr")
    os.makedirs(usage_old)
    with open(os.path.join(usage_old, "install_id"), "w", encoding="utf-8") as fh:
        fh.write("0123456789abcdef0123456789abcdef")
    cfg = make_cfg(dict(legacy))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)
    real_copy = data_paths._ps._copy_one

    def refuse_install_id(dst, sources, copied):
        if dst.endswith("install_id"):
            raise OSError("simulated refusal")
        return real_copy(dst, sources, copied)

    monkeypatch.setattr(data_paths._ps, "_copy_one", refuse_install_id)
    data_paths.bind(cfg, str(tmp_path / "plugin.py"), Log())

    migration = cfg.STORAGE_MIGRATION
    assert migration.install_id_blocked is True
    assert not os.path.exists(layout.beside(".migrated")), "a blocked pass must not settle"
    assert not os.path.exists(layout.beside(".active"))


def test_bind_with_no_layout_from_the_vendored_bind_does_not_raise(data_paths, monkeypatch, tmp_path):
    """A None from the vendored bind must not raise on layout.legacy: the process keeps the legacy
    DATA_DIR and gets a legacy layout recorded on cfg."""
    literals = source_literals()
    cfg = make_cfg(dict(literals))
    monkeypatch.setattr(data_paths._ps, "bind", lambda spec, cfg, plugin_file, logger: None)

    got = data_paths.bind(cfg, str(tmp_path / "plugin.py"), Log())

    assert got.legacy is True
    assert cfg.STORAGE_LAYOUT is got
    assert cfg.DATA_DIR == literals["DATA_DIR"]


def test_bind_with_no_returned_layout_reads_a_bound_layout_back_from_cfg(data_paths, monkeypatch, tmp_path):
    """When the vendored bind returns None but cfg holds a bound layout, bind uses that layout and
    rebinds DATA_DIR to it, the same as a normal bind."""
    literals = source_literals()
    layout = make_layout(data_paths, str(tmp_path))
    cfg = make_cfg(dict(literals, STORAGE_LAYOUT=layout))
    monkeypatch.setattr(data_paths._ps, "bind", lambda spec, cfg, plugin_file, logger: None)

    got = data_paths.bind(cfg, str(tmp_path / "plugin.py"), Log())

    assert got is layout
    assert cfg.DATA_DIR == layout.data_dir


def test_the_module_config_reads_and_writes_this_modules_globals(plugin_impl, monkeypatch):
    """The config handed to data_paths is bound to the module's own globals, not to sys.modules."""
    cfg = plugin_impl._MODULE_CFG
    with pytest.raises(AttributeError):
        _ = cfg.NO_SUCH_STATE_NAME_XYZ
    assert getattr(cfg, "NO_SUCH_STATE_NAME_XYZ", "fallback") == "fallback"
    monkeypatch.setattr(plugin_impl, "_PROBE_CFG_WRITE", "before", raising=False)
    cfg._PROBE_CFG_WRITE = "after"
    assert plugin_impl._PROBE_CFG_WRITE == "after"
    assert cfg._PROBE_CFG_WRITE == "after"


def test_a_second_module_object_under_the_same_name_does_not_capture_the_bind(
        data_paths, monkeypatch, tmp_path):
    """Startup race: while plugin.py binds, sys.modules[name] names a second, partly executed module
    object, and that object's own top-level STORAGE_LAYOUT = None runs during bind. The old code
    passed sys.modules[__name__] as the config, so bind returned None and data_paths.bind raised
    AttributeError on layout.legacy. The fixed plugin binds its own globals and is unaffected."""
    name = "dustarr_under_test.plugin_reentry"
    other = types.ModuleType(name)
    for key, value in source_literals().items():
        setattr(other, key, value)
    monkeypatch.setitem(sys.modules, name, other)
    celery_mod = sys.modules["celery"]
    monkeypatch.setattr(celery_mod, "registered_tasks", dict(celery_mod.registered_tasks))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)

    def migrate_while_the_other_module_runs(*a, **k):
        other.STORAGE_LAYOUT = None
        return None

    monkeypatch.setattr(data_paths._ps, "migrate", migrate_while_the_other_module_runs)
    namespace = {"__name__": name, "__package__": "dustarr_under_test",
                 "__file__": str(PLUGIN_SOURCE), "__builtins__": __builtins__}
    code = compile(PLUGIN_SOURCE.read_text(encoding="utf-8"), str(PLUGIN_SOURCE), "exec")

    exec(code, namespace)

    assert namespace["STORAGE_LAYOUT"] is layout
    assert namespace["DATA_DIR"] == layout.data_dir
    assert namespace["USAGE_FILE_PATH"] == os.path.join(layout.data_dir, "usage.json")
    assert namespace["LEGACY_PATHS"]["USAGE_FILE_PATH"] == "/data/dustarr/usage.json"


def test_a_data_directory_named_for_another_key_warns_once_and_still_binds(data_paths, monkeypatch, tmp_path):
    """A Hub install can change the storage key (the slug), so the directory name may differ from
    the plugin key. The bind logs one WARNING and keeps the resolved directory."""
    legacy = tmp_legacy(tmp_path)
    cfg = make_cfg(dict(legacy, DATA_DIR="/data/dustarr"))
    plugins = str(tmp_path / "plugins")
    layout = data_paths.Layout(False, os.path.join(plugins, "_data", "iptv-checker"), plugins,
                               "dustarr", "test")
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)
    log = Log()

    data_paths.bind(cfg, str(tmp_path / "plugin.py"), log)

    assert len(log.warnings) == 1
    assert "iptv-checker" in log.warnings[0]
    assert cfg.DATA_DIR == layout.data_dir


def test_a_data_directory_named_for_the_key_logs_nothing(data_paths, monkeypatch, tmp_path):
    legacy = tmp_legacy(tmp_path)
    cfg = make_cfg(dict(legacy, DATA_DIR="/data/dustarr"))
    layout = make_layout(data_paths, str(tmp_path))
    monkeypatch.setattr(data_paths._ps, "resolve", lambda *a, **k: layout)
    log = Log()

    data_paths.bind(cfg, str(tmp_path / "plugin.py"), log)

    assert log.warnings == []
