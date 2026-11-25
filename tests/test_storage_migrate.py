"""The carry-over copies the old /data state into the data directory, once, and never overwrites.

Each test builds its own old-state tree under tmp_path (tmp_legacy) and a fresh layout, so the
settled markers of one test never leak into another. Nothing reads or writes the real /data.
"""
import json
import os

import pytest

from storage_helpers import Log, make_cfg, make_layout, tmp_legacy

OLD_FILES = {
    "USAGE_FILE_PATH": b'{"channels": {}, "meta": {}}',
    "SCHEDULED_RUN_PATH": b'{"last_scheduled_run_ts": 1.0}',
    "NOTIFY_STATE_PATH": b'{"prev_ok": true}',
}


@pytest.fixture
def env(plugin_impl, tmp_path):
    data_paths = plugin_impl.data_paths
    legacy = tmp_legacy(tmp_path)
    cfg = make_cfg(dict(legacy))
    layout = make_layout(data_paths, str(tmp_path))
    items = data_paths.items_for(cfg, layout)
    new = {item.name: item.new for item in items}
    return data_paths, legacy, new, layout, items


def write_old(legacy):
    for name, data in OLD_FILES.items():
        os.makedirs(os.path.dirname(legacy[name]), exist_ok=True)
        with open(legacy[name], "wb") as fh:
            fh.write(data)


def test_every_state_file_is_copied_into_the_data_directory(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)

    result = data_paths.migrate(layout, items, Log(), version="9.9.9")

    for name, data in OLD_FILES.items():
        path = new[name]
        assert path.startswith(layout.data_dir), name
        with open(path, "rb") as fh:
            assert fh.read() == data, name
    assert len(result.copied) == len(OLD_FILES)
    assert result.failed == []


def test_the_old_files_are_left_untouched(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    data_paths.migrate(layout, items, Log())
    for name, data in OLD_FILES.items():
        with open(legacy[name], "rb") as fh:
            assert fh.read() == data, name


def test_a_new_file_is_never_overwritten(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    os.makedirs(layout.data_dir, exist_ok=True)
    with open(os.path.join(layout.data_dir, "usage.json"), "wb") as fh:
        fh.write(b'{"new": true}')

    data_paths.migrate(layout, items, Log())

    with open(new["USAGE_FILE_PATH"], "rb") as fh:
        assert fh.read() == b'{"new": true}'


def test_the_usage_reporter_folder_is_copied_under_the_dustarr_subfolder(env):
    data_paths, legacy, new, layout, items = env
    old_folder = os.path.join(legacy["USAGE_DATA_DIR"], "dustarr")
    os.makedirs(old_folder)
    with open(os.path.join(old_folder, "install_id"), "w", encoding="utf-8") as fh:
        fh.write("0123456789abcdef0123456789abcdef")
    with open(os.path.join(old_folder, "sent"), "w", encoding="utf-8") as fh:
        fh.write("")

    result = data_paths.migrate(layout, items, Log())

    new_folder = os.path.join(new["USAGE_DATA_DIR"], "dustarr")
    with open(os.path.join(new_folder, "install_id"), encoding="utf-8") as fh:
        assert fh.read() == "0123456789abcdef0123456789abcdef"
    assert os.path.exists(os.path.join(new_folder, "sent"))
    assert result.install_id_blocked is False


def test_the_old_report_fallback_folder_is_not_carried_over(env):
    data_paths, legacy, new, layout, items = env
    old_reports = os.path.join(legacy["REPORT_FALLBACK_DIR"])
    os.makedirs(old_reports)
    with open(os.path.join(old_reports, "report.html"), "w", encoding="utf-8") as fh:
        fh.write("<html></html>")

    data_paths.migrate(layout, items, Log())

    assert not os.path.exists(os.path.join(new["REPORT_FALLBACK_DIR"], "report.html"))
    assert new["REPORT_FALLBACK_DIR"] == os.path.join(layout.plugins_dir, "_shared", "newsflasharr", "dustarr")


def test_the_migrated_record_lists_each_copy_and_no_failure(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    data_paths.migrate(layout, items, Log(), version="9.9.9")
    with open(os.path.join(layout.data_dir, "migrated.json"), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["version"] == "9.9.9"
    assert record["failed"] == []
    assert {c["path"] for c in record["copied"]} == {new[n] for n in OLD_FILES}
    assert os.path.exists(layout.beside(".migrated"))
    assert os.path.exists(layout.beside(".active"))


def test_a_second_pass_in_the_same_process_is_skipped(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    data_paths.migrate(layout, items, Log())
    again = data_paths.migrate(layout, items, Log())
    assert again.skipped_pass is True
    assert again.copied == []


def test_a_failed_copy_is_reported_and_does_not_settle_the_markers(env, monkeypatch):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    real_copy = data_paths._ps._copy_one

    def refuse_usage(dst, sources, copied):
        if dst.endswith("usage.json"):
            raise OSError("simulated refusal")
        return real_copy(dst, sources, copied)

    monkeypatch.setattr(data_paths._ps, "_copy_one", refuse_usage)
    log = Log()
    result = data_paths.migrate(layout, items, log)

    assert "usage.json" in result.failed
    assert log.errors, "a failed copy must be logged"
    assert not os.path.exists(layout.beside(".migrated")), "a pass with a failure must retry next start"
    assert not os.path.exists(layout.beside(".active"))


def test_a_legacy_layout_copies_nothing(env):
    data_paths, legacy, new, layout, items = env
    write_old(legacy)
    legacy_layout = data_paths.Layout(True, None, None, "dustarr", "test")
    result = data_paths.migrate(legacy_layout, items, Log())
    assert result.skipped_pass is True
    assert not os.path.exists(new["USAGE_FILE_PATH"]), "a legacy layout must not write the new path"
