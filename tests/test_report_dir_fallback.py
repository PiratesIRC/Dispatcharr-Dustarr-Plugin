"""The report falls back to /data when /config cannot be written.

Dispatcharr's plugin guide (Plugins.md, read 2026-10-17) asks plugins to write
only under /data or /app/data, and a stock Dispatcharr image has no writable
/config. This installation bind-mounts /config to the host, which is why the
report lives there. _writable_dir keeps /config/dustarr when it can be written
and otherwise uses REPORT_FALLBACK_DIR, so Build Report still publishes a file
on a stock installation.
"""
import sys

import pytest
from conftest import load_plugin

NOW = 1_700_000_000.0


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module


def test_the_preferred_directory_is_used_when_writable(plugin, tmp_path):
    preferred = str(tmp_path / "config" / "dustarr")
    assert plugin._writable_dir(preferred, str(tmp_path / "fallback")) == preferred


def test_the_fallback_is_used_when_the_preferred_directory_cannot_be_written(plugin, tmp_path):
    fallback = str(tmp_path / "fallback")
    assert plugin._writable_dir("/nonexistent\x00dir", fallback) == fallback


def test_the_preferred_directory_is_returned_when_neither_can_be_used(plugin):
    assert plugin._writable_dir("/nonexistent\x00a", "/nonexistent\x00b") == "/nonexistent\x00a"


def test_the_fallback_is_under_data_and_never_under_logos(plugin):
    assert plugin.REPORT_FALLBACK_DIR.startswith("/data/")
    assert "/logos" not in plugin.REPORT_FALLBACK_DIR


def test_build_report_publishes_to_the_fallback(plugin, tmp_path, monkeypatch):
    gw_mod = sys.modules["dustarr_under_test.gateway"]
    storage_mod = sys.modules["dustarr_under_test.storage"]
    rows = [gw_mod.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                              auto_created=False, created_at=NOW - 90 * 86400,
                              proxying=True) for i in range(5)]

    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return rows

    storage_mod.Storage(str(tmp_path / "data")).write(
        {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}, NOW)
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", "/nonexistent\x00dir")
    monkeypatch.setattr(plugin, "CSV_DIR", "/nonexistent\x00dir")
    monkeypatch.setattr(plugin, "REPORT_FALLBACK_DIR", str(fallback))
    monkeypatch.setattr(plugin, "_gateway", lambda: FakeGateway())

    result = plugin.Plugin().run("build_report", {}, {"settings": {}})

    assert result["status"] == "ok", result
    assert (fallback / "report.html").exists()
    assert result["file"].startswith(str(fallback))
