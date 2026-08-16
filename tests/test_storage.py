import json

import pytest
from conftest import load_pure


@pytest.fixture()
def storage_mod():
    return load_pure("storage")


@pytest.fixture()
def store(storage_mod, tmp_path):
    return storage_mod.Storage(str(tmp_path))


def test_load_missing_returns_empty(store):
    assert store.load(1000.0) == {}


def test_write_then_load_roundtrip(store):
    payload = {"channels": {"u1": {"watch_count": 2}}, "meta": {"stats_since": 500.0}}
    assert store.write(payload, 1000.0) is True
    loaded = store.load(1001.0)
    assert loaded["channels"]["u1"]["watch_count"] == 2
    assert loaded["written_at"] == 1000.0


def test_write_is_atomic_no_tmp_left_behind(store, tmp_path):
    store.write({"channels": {}}, 1000.0)
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / "usage.json").exists()


def test_corrupt_file_is_sidelined_not_blanked(store, storage_mod, tmp_path):
    (tmp_path / "usage.json").write_text("{not json", encoding="utf-8")
    assert store.load(1234.0) == {}
    sidelined = list(tmp_path.glob("usage.json.corrupt-*"))
    assert len(sidelined) == 1
    # The bad bytes are PRESERVED for post-mortem, never silently discarded.
    assert sidelined[0].read_text(encoding="utf-8") == "{not json"
    assert store.stats["corrupt_sidelines"] == 1


def test_stats_since_set_once_and_never_moved(store):
    payload = store.ensure_stats_since({}, 1000.0)
    assert payload["meta"]["stats_since"] == 1000.0
    again = store.ensure_stats_since(payload, 9999.0)
    assert again["meta"]["stats_since"] == 1000.0  # NOT moved forward


def test_write_fsyncs_the_temp_file_before_replacing(store, storage_mod,
                                                     monkeypatch):
    """Without flush+fsync before os.replace, a power loss inside the
    filesystem journal window can publish a truncated usage.json; load()
    then sidelines it and silently restarts the dataset."""
    calls = []
    real_fsync = storage_mod.os.fsync
    real_replace = storage_mod.os.replace

    def spy_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(storage_mod.os, "fsync", spy_fsync)
    monkeypatch.setattr(storage_mod.os, "replace", spy_replace)
    assert store.write({"channels": {}}, 1000.0) is True
    assert calls == ["fsync", "replace"]


def test_stats_since_rearms_after_corrupt_sideline(store, tmp_path):
    # A recreated usage.json must get a FRESH stats_since, which re-arms the
    # 30-day blackout. Carrying it forward would let the report act on an
    # empty dataset (spec S6).
    store.write(store.ensure_stats_since({}, 1000.0), 1000.0)
    (tmp_path / "usage.json").write_text("garbage", encoding="utf-8")
    recreated = store.ensure_stats_since(store.load(5000.0), 5000.0)
    assert recreated["meta"]["stats_since"] == 5000.0


def test_circuit_breaker_opens_after_repeated_io_failures(store, storage_mod,
                                                          monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    for _ in range(storage_mod.CIRCUIT_FAILS):
        assert store.write({"channels": {}}, 1000.0) is False
    # Circuit now open: further writes are dropped WITHOUT touching the fs.
    monkeypatch.setattr(storage_mod.os, "replace",
                        lambda *a, **k: pytest.fail("wrote while circuit open"))
    assert store.write({"channels": {}}, 1001.0) is False
    assert store.stats["dropped_writes"] >= storage_mod.CIRCUIT_FAILS + 1


def test_circuit_closes_after_retry_window(store, storage_mod, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    for _ in range(storage_mod.CIRCUIT_FAILS):
        store.write({"channels": {}}, 1000.0)
    monkeypatch.undo()
    later = 1000.0 + storage_mod.CIRCUIT_RETRY_S + 1
    assert store.write({"channels": {}}, later) is True


def test_written_file_is_valid_json_on_disk(store, tmp_path):
    store.write({"channels": {"u1": {}}, "meta": {}}, 1000.0)
    on_disk = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert on_disk["channels"] == {"u1": {}}


def test_write_cleans_up_tmp_on_replace_failure(store, storage_mod, tmp_path,
                                                 monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    assert store.write({"channels": {}}, 1000.0) is False
    # The failed os.replace must not leave usage.json.tmp behind as debris.
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_sidelines_not_counted_when_rename_fails(store, storage_mod,
                                                          tmp_path, monkeypatch):
    (tmp_path / "usage.json").write_text("{not json", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(storage_mod.os, "replace", boom)
    assert store.load(1234.0) == {}
    # The sideline rename never happened, so the stat must not claim it did.
    assert store.stats["corrupt_sidelines"] == 0
    # The corrupt file stays exactly where it was, bytes untouched.
    assert (tmp_path / "usage.json").read_text(encoding="utf-8") == "{not json"


def test_sideline_collision_preserves_both_corrupt_files(store, tmp_path):
    (tmp_path / "usage.json").write_text("first corrupt", encoding="utf-8")
    assert store.load(2000.0) == {}
    (tmp_path / "usage.json").write_text("second corrupt", encoding="utf-8")
    # Same integer second as the first corruption: must not clobber it.
    assert store.load(2000.0) == {}

    sidelined = sorted(tmp_path.glob("usage.json.corrupt-2000*"))
    assert len(sidelined) == 2
    contents = {p.read_text(encoding="utf-8") for p in sidelined}
    assert contents == {"first corrupt", "second corrupt"}
    assert store.stats["corrupt_sidelines"] == 2
