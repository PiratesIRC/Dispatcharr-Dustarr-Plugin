"""Rollout safety: old-code and new-code workers run side by side for one release.

data_paths.ROLLOUT_DUAL_PATHS is True in this release. While it is, a bound worker adopts an old
/data copy of a state file when that copy is the newer state, and never adopts a corrupt one. The
usage collector holds a box-wide lease, so a new-code leader reads the old usage.json on every
acquisition through Storage.load: the old leader's sessions since the carry-over are kept.
"""
import json
import os
import time

import pytest
from conftest import FakeClock, FakeRedis, load_pure
from storage_helpers import make_layout, tmp_legacy

THRESHOLDS = {"min_watch_seconds": 120.0, "client_gap_grace_s": 90.0,
              "merge_gap_s": 120.0, "poll_interval_s": 15.0}
NOW = 1_700_000_000.0
UUID_OLD = "00000000-0000-4000-8000-0000000000aa"


@pytest.fixture
def storage(plugin_impl):
    return plugin_impl.storage


@pytest.fixture
def bound(plugin_impl, monkeypatch, tmp_path):
    """The plugin module as if bind had succeeded: a non-legacy layout, legacy copies under tmp."""
    legacy = tmp_legacy(tmp_path)
    layout = make_layout(plugin_impl.data_paths, str(tmp_path / "root"))
    monkeypatch.setattr(plugin_impl, "LEGACY_PATHS", dict(legacy))
    monkeypatch.setattr(plugin_impl, "STORAGE_LAYOUT", layout)
    monkeypatch.setattr(plugin_impl, "DATA_DIR", layout.data_dir)
    monkeypatch.setattr(plugin_impl, "USAGE_FILE_PATH", os.path.join(layout.data_dir, "usage.json"))
    monkeypatch.setattr(plugin_impl, "SCHEDULED_RUN_PATH", os.path.join(layout.data_dir, "scheduled_run.json"))
    monkeypatch.setattr(plugin_impl, "NOTIFY_STATE_PATH", os.path.join(layout.data_dir, "notify_state.json"))
    os.makedirs(layout.data_dir, exist_ok=True)
    return layout, legacy


def put(path, obj, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.utime(path, (mtime, mtime))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def usage_with(uuid, watch_count):
    return {"channels": {uuid: {"watch_count": watch_count}}, "meta": {"stats_since": NOW}}


def test_the_flag_names_the_old_path_only_for_a_bound_worker(plugin_impl, monkeypatch, bound):
    data_paths = plugin_impl.data_paths
    assert data_paths.legacy_for(plugin_impl, "USAGE_FILE_PATH") == plugin_impl.LEGACY_PATHS["USAGE_FILE_PATH"]
    monkeypatch.setattr(data_paths, "ROLLOUT_DUAL_PATHS", False)
    assert data_paths.legacy_for(plugin_impl, "USAGE_FILE_PATH") is None


def test_a_legacy_worker_adopts_nothing(plugin_impl, monkeypatch):
    legacy_layout = plugin_impl.data_paths.Layout(True, None, None, "dustarr", "test")
    monkeypatch.setattr(plugin_impl, "STORAGE_LAYOUT", legacy_layout)
    assert plugin_impl.data_paths.legacy_for(plugin_impl, "USAGE_FILE_PATH") is None


def test_a_newer_old_usage_file_is_adopted_on_load(storage, bound, plugin_impl):
    layout, legacy = bound
    new_path = plugin_impl.USAGE_FILE_PATH
    put(new_path, usage_with(UUID_OLD, 1), NOW - 600)
    put(legacy["USAGE_FILE_PATH"], usage_with(UUID_OLD, 9), NOW - 10)
    store = storage.Storage(layout.data_dir, legacy_path=legacy["USAGE_FILE_PATH"])

    data = store.load(NOW)

    assert data["channels"][UUID_OLD]["watch_count"] == 9
    assert read(new_path)["channels"][UUID_OLD]["watch_count"] == 9
    assert store.stats["legacy_adoptions"] == 1


def test_an_older_old_usage_file_never_replaces_the_new_one(storage, bound, plugin_impl):
    layout, legacy = bound
    new_path = plugin_impl.USAGE_FILE_PATH
    put(new_path, usage_with(UUID_OLD, 5), NOW - 10)
    put(legacy["USAGE_FILE_PATH"], usage_with(UUID_OLD, 1), NOW - 600)
    store = storage.Storage(layout.data_dir, legacy_path=legacy["USAGE_FILE_PATH"])
    assert store.load(NOW)["channels"][UUID_OLD]["watch_count"] == 5
    assert store.stats["legacy_adoptions"] == 0


def test_a_corrupt_old_file_never_overwrites_a_good_new_one(storage, bound, plugin_impl):
    layout, legacy = bound
    new_path = plugin_impl.USAGE_FILE_PATH
    put(new_path, usage_with(UUID_OLD, 5), NOW - 600)
    os.makedirs(os.path.dirname(legacy["USAGE_FILE_PATH"]), exist_ok=True)
    with open(legacy["USAGE_FILE_PATH"], "w", encoding="utf-8") as fh:
        fh.write("{not json")
    os.utime(legacy["USAGE_FILE_PATH"], (NOW - 1, NOW - 1))
    store = storage.Storage(layout.data_dir, legacy_path=legacy["USAGE_FILE_PATH"])

    assert store.load(NOW)["channels"][UUID_OLD]["watch_count"] == 5
    assert read(new_path)["channels"][UUID_OLD]["watch_count"] == 5


def test_a_corrupt_new_file_is_sidelined_and_a_valid_old_one_adopted(storage, bound, plugin_impl):
    layout, legacy = bound
    new_path = plugin_impl.USAGE_FILE_PATH
    with open(new_path, "w", encoding="utf-8") as fh:
        fh.write("{truncated")
    put(legacy["USAGE_FILE_PATH"], usage_with(UUID_OLD, 7), NOW - 600)
    store = storage.Storage(layout.data_dir, legacy_path=legacy["USAGE_FILE_PATH"])

    assert store.load(NOW)["channels"][UUID_OLD]["watch_count"] == 7
    sidelined = [n for n in os.listdir(layout.data_dir) if n.startswith("usage.json.corrupt-")]
    assert len(sidelined) == 1, "the corrupt bytes are renamed aside, never deleted"


def test_no_adoption_without_a_legacy_path(storage, bound, plugin_impl):
    layout, legacy = bound
    put(legacy["USAGE_FILE_PATH"], usage_with(UUID_OLD, 9), NOW - 10)
    store = storage.Storage(layout.data_dir)
    assert store.load(NOW) == {}
    assert store.stats["legacy_adoptions"] == 0


def test_the_lease_acquisition_reads_the_old_leader_state(storage, bound, plugin_impl):
    """run_tick acquires the free lease, _acquire_state loads through Storage.load, and the old
    leader's usage.json is adopted there, so the sessions it recorded are in the new collector."""
    layout, legacy = bound
    put(legacy["USAGE_FILE_PATH"], usage_with(UUID_OLD, 4), NOW - 10)
    col_mod = load_pure("collector")
    sess_mod = load_pure("sessionizer")
    clock = FakeClock(NOW)
    redis = FakeRedis(clock=clock)
    sess = sess_mod.Sessionizer(dict(THRESHOLDS))
    store = storage.Storage(layout.data_dir, legacy_path=legacy["USAGE_FILE_PATH"])
    col = col_mod.Collector(redis, sess, store, dict(THRESHOLDS), token="me", wall=clock.wall)

    col.run_tick()

    assert UUID_OLD in col.sessionizer.channels
    assert col.sessionizer.channels[UUID_OLD]["watch_count"] == 4


def test_the_scheduled_run_stamp_adopts_a_newer_old_run(plugin_impl, bound):
    layout, legacy = bound
    put(legacy["SCHEDULED_RUN_PATH"], {"last_scheduled_run_ts": NOW}, NOW)
    assert plugin_impl._read_scheduled_run_ts() == NOW


def test_the_notify_gate_state_adopts_a_newer_old_file(plugin_impl, bound):
    layout, legacy = bound
    put(legacy["NOTIFY_STATE_PATH"], {"prev_ok": False}, NOW)
    plugin_impl._adopt_rollout_file(plugin_impl.NOTIFY_STATE_PATH, "NOTIFY_STATE_PATH")
    assert read(plugin_impl.NOTIFY_STATE_PATH) == {"prev_ok": False}


def test_nothing_is_adopted_once_the_flag_is_off(plugin_impl, bound, monkeypatch):
    layout, legacy = bound
    monkeypatch.setattr(plugin_impl.data_paths, "ROLLOUT_DUAL_PATHS", False)
    put(legacy["SCHEDULED_RUN_PATH"], {"last_scheduled_run_ts": NOW}, NOW)
    assert plugin_impl._read_scheduled_run_ts() is None
    assert not os.path.exists(plugin_impl.SCHEDULED_RUN_PATH)


def test_the_report_counter_is_never_adopted(plugin_impl, bound):
    """The counter path is the same in both releases, so there is nothing to adopt: a bound
    worker reads and writes the one literal file."""
    layout, legacy = bound
    from storage_helpers import source_literals
    assert source_literals()["REPORT_COUNT_PATH"] == "/data/dustarr/report_count.json"
    assert "REPORT_COUNT_PATH" not in plugin_impl.LEGACY_PATHS
