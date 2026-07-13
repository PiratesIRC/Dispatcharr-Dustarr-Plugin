import logging
import sys
import threading
import time

import pytest
from conftest import load_plugin

NOW = 1_700_000_000.0


@pytest.fixture()
def plugin():
    return load_plugin()


def _make_thread(plugin, version=None, alive_for=30.0):
    """A lightweight stand-in for a collector thread: the right name/version/
    stop-event shape that ensure_collector's bookkeeping inspects, without any
    of the real Redis-polling work. Caller must set() the stop event and
    join() when done (tests do this in a `finally`)."""
    stop_event = threading.Event()

    def loop():
        stop_event.wait(alive_for)

    thread = threading.Thread(target=loop, name=plugin.THREAD_NAME, daemon=True)
    thread.metricsarr_version = version or plugin.PLUGIN_VERSION
    thread.metricsarr_stop = stop_event
    thread.start()
    return thread


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
    # show_summary only touches Storage(DATA_DIR) today, but patch all three
    # real-path globals here too (defense in depth / FIX 3 audit) so a future
    # change that routes it through _build_report can't silently write to the
    # real host paths (/data/logos/metricsarr, /config/metricsarr) again.
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    result = plugin.Plugin().run("show_summary", {}, {"settings": {}})
    assert result["status"] in ("ok", "error")
    assert result["message"]


def test_send_webhook_now_errors_without_a_url(plugin, tmp_path, monkeypatch):
    # send_webhook_now runs _build_report() first (to build the summary),
    # which writes real files via REPORT_DIR/CSV_DIR unless patched -- without
    # this, the suite was writing to C:\data\logos\metricsarr and
    # C:\config\metricsarr on the host (FIX 3).
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    result = plugin.Plugin().run("send_webhook_now", {}, {"settings": {}})
    assert result["status"] == "error"
    assert "webhook" in result["message"].lower()


def test_actions_expose_the_expected_ids(plugin):
    ids = {a["id"] for a in plugin.ACTIONS}
    assert {"build_report", "show_summary", "send_webhook_now",
            "validate_settings"} <= ids


# ---- C1: build_report_task must actually register with Celery --------------

def test_build_report_task_is_registered_with_celery(plugin):
    celery_mod = sys.modules["celery"]
    task = plugin.build_report_task
    assert task.name in celery_mod.registered_tasks
    assert celery_mod.registered_tasks[task.name] is task


def test_build_report_task_runs_and_returns_counts(plugin, tmp_path, monkeypatch):
    import sys as _sys
    gw_mod = _sys.modules["metricsarr_under_test.gateway"]

    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return []

    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", lambda: FakeGateway())
    monkeypatch.setattr(plugin, "_load_settings", lambda: {})

    counts = plugin.build_report_task()
    assert "never_watched" in counts
    del gw_mod  # imported only to document the module is loaded; unused otherwise


def test_build_report_task_never_raises_and_redacts_the_log(plugin, monkeypatch,
                                                            caplog):
    def boom():
        raise RuntimeError("http://host/live/topsecretuser/topsecretpass/x")

    monkeypatch.setattr(plugin, "_load_settings", boom)
    with caplog.at_level(logging.ERROR):
        plugin.build_report_task()          # must not raise
    assert "topsecretpass" not in caplog.text


# ---- C2: Plugin.stop() ------------------------------------------------------

def test_stop_deletes_the_periodic_task(plugin):
    core_sched = sys.modules["core.scheduling"]
    core_sched.delete_periodic_task.reset_mock()
    plugin.Plugin().stop()
    core_sched.delete_periodic_task.assert_called_once_with(plugin.TASK_NAME)


def test_stop_stops_the_collector_thread(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    spawned = []

    def fake_spawn(settings):
        thread = _make_thread(plugin)
        spawned.append(thread)
        return thread

    monkeypatch.setattr(plugin, "_spawn_collector", fake_spawn)
    plugin.ensure_collector({})
    thread = spawned[0]
    try:
        plugin.Plugin().stop()
        assert thread.metricsarr_stop.is_set()
    finally:
        thread.metricsarr_stop.set()
        thread.join(timeout=2)


def test_stop_never_raises_even_if_scheduling_cleanup_fails(plugin, monkeypatch):
    core_sched = sys.modules["core.scheduling"]

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(core_sched, "delete_periodic_task", boom)
    plugin.Plugin().stop()               # must not raise


# ---- I1: ensure_collector's riskiest branches -------------------------------

def test_ensure_collector_noop_outside_uwsgi(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: False)
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    plugin.ensure_collector({})
    assert spawned == []


def test_ensure_collector_is_idempotent_one_thread_per_worker(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    spawned = []

    def fake_spawn(settings):
        thread = _make_thread(plugin)
        spawned.append(thread)
        return thread

    monkeypatch.setattr(plugin, "_spawn_collector", fake_spawn)
    try:
        plugin.ensure_collector({})
        plugin.ensure_collector({})
        assert len(spawned) == 1

        live = [t for t in threading.enumerate()
                if t.name == plugin.THREAD_NAME and t.is_alive()]
        assert len(live) == 1
    finally:
        for thread in spawned:
            thread.metricsarr_stop.set()
            thread.join(timeout=2)


def test_ensure_collector_supersedes_an_old_version(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    old = _make_thread(plugin, version="old-version")
    spawned = []

    def fake_spawn(settings):
        thread = _make_thread(plugin)
        spawned.append(thread)
        return thread

    monkeypatch.setattr(plugin, "_spawn_collector", fake_spawn)
    try:
        plugin.ensure_collector({})
        assert old.metricsarr_stop.is_set()
        assert len(spawned) == 1
    finally:
        old.metricsarr_stop.set()
        old.join(timeout=2)
        for thread in spawned:
            thread.metricsarr_stop.set()
            thread.join(timeout=2)


def test_ensure_collector_crash_loop_bound_refuses_a_new_spawn(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    now = time.time()
    monkeypatch.setattr(plugin, "_restart_times", [now] * plugin.RESTART_BOUND)

    plugin.ensure_collector({})
    assert spawned == []


def test_ensure_collector_crash_loop_bound_does_not_kill_the_incumbent(plugin,
                                                                       monkeypatch):
    """M3: the crash-loop bound must be checked BEFORE the incumbent thread is
    stopped -- otherwise a 6th version-bump within an hour kills the running
    collector and refuses to replace it, leaving the worker with none."""
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    old = _make_thread(plugin, version="old-version")
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    now = time.time()
    monkeypatch.setattr(plugin, "_restart_times", [now] * plugin.RESTART_BOUND)

    try:
        plugin.ensure_collector({})
        assert spawned == []
        assert not old.metricsarr_stop.is_set()
        assert old.is_alive()
    finally:
        old.metricsarr_stop.set()
        old.join(timeout=2)


def test_run_swallows_a_collector_spawn_failure(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))

    def boom(settings):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(plugin, "ensure_collector", boom)
    result = plugin.Plugin().run("show_summary", {}, {"settings": {}})
    assert result["status"] in ("ok", "error")
    assert result["message"]


def test_run_redacts_exception_text_in_the_catch_all(plugin, monkeypatch):
    def boom(settings):
        raise RuntimeError("http://host/live/secretuser/secretpass/foo")

    monkeypatch.setattr(plugin, "_build_report", boom)
    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["status"] == "error"
    assert "secretpass" not in result["message"]


# ---- I2: _validate must redact both its own messages ------------------------

def test_validate_settings_redacts_scheduling_failure_message(plugin, monkeypatch):
    def boom(settings):
        raise RuntimeError("http://host/live/schedcredsuser/schedcredspass/x")

    monkeypatch.setattr(plugin, "sync_schedule", boom)
    result = plugin.Plugin()._validate({})
    assert "schedcredspass" not in result["message"]


def test_validate_settings_redacts_regex_compile_error_message(plugin, monkeypatch):
    import re as re_mod

    def fake_compile(pattern):
        raise re_mod.error("bad pattern near "
                           "http://host/live/regexcredsuser/regexcredspass/x")

    monkeypatch.setattr(re_mod, "compile", fake_compile)
    result = plugin.Plugin()._validate({"exclude_name_regex": "anything"})
    assert result["status"] == "error"
    assert "regexcredspass" not in result["message"]


# ---- I3: sync_schedule must run on every action, not just Validate ----------

def test_build_report_action_arms_the_schedule(plugin, tmp_path, monkeypatch):
    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return []

    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", lambda: FakeGateway())

    core_sched = sys.modules["core.scheduling"]
    core_sched.create_or_update_periodic_task.reset_mock()

    plugin.Plugin().run("build_report", {}, {"settings": {"report_schedule": "daily"}})
    assert core_sched.create_or_update_periodic_task.called


def test_sync_schedule_failure_never_breaks_an_action(plugin, tmp_path, monkeypatch):
    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return []

    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", lambda: FakeGateway())

    def boom(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "sync_schedule", boom)
    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["status"] == "ok"


# ---- I4: select values must be validated against their declared options ----

def test_coerce_settings_falls_back_to_default_for_invalid_report_schedule(plugin):
    out = plugin.coerce_settings({"report_schedule": "hourly"})
    assert out["report_schedule"] == "weekly"       # field default


def test_coerce_settings_falls_back_to_default_for_invalid_webhook_format(plugin):
    out = plugin.coerce_settings({"webhook_format": "xml"})
    assert out["webhook_format"] == "discord"        # field default


def test_coerce_settings_keeps_a_valid_select_value(plugin):
    out = plugin.coerce_settings({"report_schedule": "daily"})
    assert out["report_schedule"] == "daily"


def test_invalid_report_schedule_does_not_silently_disable_scheduling(plugin,
                                                                      monkeypatch):
    """I4's smoking gun: report_schedule='hourly' must NOT resolve to 'off'
    scheduling. sync_schedule should treat it the same as leaving the field
    at its default (weekly), not as a request to delete the Beat entry."""
    core_sched = sys.modules["core.scheduling"]
    core_sched.delete_periodic_task.reset_mock()
    core_sched.create_or_update_periodic_task.reset_mock()

    plugin.sync_schedule({"report_schedule": "hourly"})

    assert core_sched.create_or_update_periodic_task.called
    assert not core_sched.delete_periodic_task.called


# ---- Minor findings ----------------------------------------------------------

@pytest.mark.parametrize("bad", ["a string", 5, ["x"], 3.14, True, ("t",)])
def test_coerce_settings_tolerates_non_dict_input(plugin, bad):
    out = plugin.coerce_settings(bad)
    assert out["poll_interval_s"] == 15


def test_coerce_settings_falls_back_to_default_on_nan(plugin):
    out = plugin.coerce_settings({"poll_interval_s": float("nan")})
    assert out["poll_interval_s"] == 15


def test_coerce_settings_falls_back_to_default_on_positive_infinity(plugin):
    out = plugin.coerce_settings({"unused_threshold_days": float("inf")})
    assert out["unused_threshold_days"] == 30


def test_coerce_settings_falls_back_to_default_on_negative_infinity(plugin):
    out = plugin.coerce_settings({"poll_interval_s": float("-inf")})
    assert out["poll_interval_s"] == 15
