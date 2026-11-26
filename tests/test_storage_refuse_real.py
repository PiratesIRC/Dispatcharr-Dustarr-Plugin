"""The test harness can never reach Dispatcharr's real /data.

conftest arms the vendored storage layer's REFUSE_REAL_DATA switch and redirects every state
constant the plugin holds into the test's tmp_path, so a write aimed at /data raises instead of
touching the box. The first test asserts the switch itself, before any write is attempted, so a
missing switch fails without writing anything.
"""
import os

import pytest
from conftest import load_plugin


@pytest.fixture()
def plugin_storage():
    return load_plugin().data_paths._ps


def test_the_refuse_real_data_switch_is_armed(plugin_storage):
    assert plugin_storage.REFUSE_REAL_DATA is True


def test_a_write_aimed_at_real_data_raises_and_creates_nothing(plugin_storage):
    assert plugin_storage.REFUSE_REAL_DATA is True
    target = "/data/dustarr/refuse_probe.json"
    with pytest.raises(RuntimeError, match="refuses to touch real Dispatcharr data"):
        plugin_storage.atomic_write_bytes(target, b"x")
    assert not os.path.exists(target)


def test_every_state_path_the_plugin_holds_is_under_tmp(tmp_path):
    plugin = load_plugin()
    names = ["DATA_DIR", "REPORT_FALLBACK_DIR", "USAGE_FILE_PATH", "SCHEDULED_RUN_PATH",
             "REPORT_COUNT_PATH", "NOTIFY_STATE_PATH"]
    values = {name: getattr(plugin, name) for name in names}
    values.update({f"LEGACY_PATHS[{k}]": v for k, v in plugin.LEGACY_PATHS.items()})
    assert plugin.LEGACY_PATHS, "bind must have recorded the legacy paths"
    for name, value in values.items():
        assert str(value).replace("\\", "/").startswith(str(tmp_path).replace("\\", "/")), name
