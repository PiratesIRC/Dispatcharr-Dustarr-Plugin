import pytest
from conftest import load_plugin

NOW = 1_700_000_000.0


@pytest.fixture()
def plugin():
    return load_plugin()


def test_coerce_settings_floors_and_coerces_junk(plugin):
    out = plugin.coerce_settings({"poll_interval_s": "not a number",
                                  "min_watch_seconds": "300",
                                  "top_n": -5})
    assert out["poll_interval_s"] == 15          # falls back to the field default
    assert out["min_watch_seconds"] == 300.0     # numeric string is accepted
    assert out["top_n"] >= 1                     # floored


def test_coerce_settings_keeps_poll_interval_under_the_metadata_ttl(plugin):
    """Dispatcharr refreshes the metadata key's 30s TTL every second. A poll
    slower than that can miss a live channel entirely (fact-check #3)."""
    out = plugin.coerce_settings({"poll_interval_s": 120})
    assert out["poll_interval_s"] <= 25


def test_fields_include_every_setting_the_code_reads(plugin):
    ids = {f["id"] for f in plugin.FIELDS}
    required = {"poll_interval_s", "min_watch_seconds", "client_gap_grace_s",
                "merge_gap_s", "top_n", "never_watched_ceiling",
                "unused_threshold_days", "exclude_auto_created", "exclude_groups",
                "exclude_name_regex", "webhook_url", "webhook_format",
                "report_schedule"}
    assert required <= ids


def test_webhook_url_field_is_masked(plugin):
    field = next(f for f in plugin.FIELDS if f["id"] == "webhook_url")
    assert field.get("input_type") == "password"


def test_select_fields_use_string_option_values(plugin):
    for field in plugin.FIELDS:
        if field["type"] == "select":
            for option in field["options"]:
                assert isinstance(option["value"], str)


def test_unknown_action_is_an_error_not_a_crash(plugin):
    result = plugin.Plugin().run("no_such_action", {}, {"settings": {}})
    assert result["status"] == "error"


def test_validate_settings_reports_a_broken_regex(plugin):
    result = plugin.Plugin().run("validate_settings", {},
                                 {"settings": {"exclude_name_regex": "((("}})
    assert result["status"] == "error"
    assert "regex" in result["message"].lower()


def test_validate_settings_passes_on_defaults(plugin):
    result = plugin.Plugin().run("validate_settings", {}, {"settings": {}})
    assert result["status"] == "ok"


def test_build_report_writes_files_and_returns_the_path(plugin, tmp_path,
                                                        monkeypatch):
    import sys
    gw_mod = sys.modules["metricsarr_under_test.gateway"]
    storage_mod = sys.modules["metricsarr_under_test.storage"]

    rows = [gw_mod.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                              auto_created=False, created_at=NOW - 90 * 86400,
                              proxying=True) for i in range(5)]

    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return rows

    store = storage_mod.Storage(str(tmp_path / "data"))
    store.write({"channels": {f"u{i}": {"watch_count": 4, "watch_seconds": 7200.0,
                                        "tune_count": 4, "last_watched": NOW - 3600,
                                        "last_tuned": NOW - 3600,
                                        "first_seen": NOW - 80 * 86400}
                              for i in range(6)},
                 "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}, NOW)

    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", lambda: FakeGateway())

    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["status"] == "ok"
    assert result["file"]                       # the card renders "Output: <path>"
    assert (tmp_path / "logos" / "report.html").exists()
    assert "never watched" in result["message"].lower()


def test_show_summary_never_crashes_on_a_missing_usage_file(plugin, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "nothing-here"))
    result = plugin.Plugin().run("show_summary", {}, {"settings": {}})
    assert result["status"] in ("ok", "error")
    assert result["message"]


def test_send_webhook_now_errors_without_a_url(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path))
    result = plugin.Plugin().run("send_webhook_now", {}, {"settings": {}})
    assert result["status"] == "error"
    assert "webhook" in result["message"].lower()


def test_actions_expose_the_expected_ids(plugin):
    ids = {a["id"] for a in plugin.ACTIONS}
    assert {"build_report", "show_summary", "send_webhook_now",
            "validate_settings"} <= ids
