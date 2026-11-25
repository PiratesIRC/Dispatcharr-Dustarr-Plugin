"""data_paths.bind rebinds the plugin's state constants to the data directory, once per import.

These tests drive data_paths.bind with a fake layout (the vendored resolve is replaced) and with
the legacy literals read from the source, so they never touch a real /data path.
"""
import ast
import os
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
    assert cfg.REPORT_COUNT_PATH == os.path.join(layout.data_dir, "report_count.json")
    assert cfg.NOTIFY_STATE_PATH == os.path.join(layout.data_dir, "notify_state.json")
    assert cfg.USAGE_DATA_DIR == os.path.join(layout.data_dir, "usage")
    # The report fallback is the shared Newsflasharr folder, not the data directory.
    assert cfg.REPORT_FALLBACK_DIR == os.path.join(layout.plugins_dir, "_shared", "newsflasharr", "dustarr")
    assert cfg.STORAGE_MIGRATION is not None


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
    assert plugin_impl.DATA_DIR == "/data/dustarr"


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
