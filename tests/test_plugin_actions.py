import logging
import os
import sys
import threading
import time
import traceback

import pytest
from conftest import load_plugin

NOW = 1_700_000_000.0


@pytest.fixture()
def plugin():
    return load_plugin()


def _make_thread(plugin, version=None, fingerprint=None, alive_for=30.0):
    """A lightweight stand-in for a collector thread: the right name/version/
    fingerprint/stop-event shape that ensure_collector's bookkeeping inspects,
    without any of the real Redis-polling work. Caller must set() the stop
    event and join() when done (tests do this in a `finally`).

    `fingerprint` defaults to the fingerprint of the DEFAULT settings ({}) --
    i.e. "this stand-in thread was spawned with today's current settings" --
    matching the common case where a test's `ensure_collector({})` call
    should be treated as a no-op repeat, not a settings change (I1)."""
    stop_event = threading.Event()

    def loop():
        stop_event.wait(alive_for)

    thread = threading.Thread(target=loop, name=plugin.THREAD_NAME, daemon=True)
    thread.metricsarr_version = version or plugin.PLUGIN_VERSION
    thread.metricsarr_fingerprint = (
        fingerprint if fingerprint is not None
        else plugin._thresholds_fingerprint(plugin.coerce_settings({})))
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


# ---- Task 6: notify_enabled must coerce to a REAL bool ----------------------
#
# Settings arrive unvalidated (a form post or a raw API write), and
# bool("false") is True in plain Python -- any non-empty string is truthy.
# A later task's caller-guard depends on
# `coerce_settings(settings).get("notify_enabled")` being a genuine bool, so a
# string "false" silently turning the toggle back ON would be exactly this
# codebase's signature failure mode (bug-139's shape, one field over).

def test_coerce_settings_notify_enabled_defaults_to_false(plugin):
    out = plugin.coerce_settings({})
    assert out["notify_enabled"] is False


def test_coerce_settings_notify_enabled_accepts_a_real_bool(plugin):
    assert plugin.coerce_settings({"notify_enabled": True})["notify_enabled"] is True
    assert plugin.coerce_settings({"notify_enabled": False})["notify_enabled"] is False


def test_coerce_settings_notify_enabled_string_false_is_false(plugin):
    """The trap: bool("false") is True. A form/API value of the literal
    string "false" must still resolve to a real False, not be coerced to
    truthy-because-non-empty."""
    for falsy in ("false", "False", "FALSE", "0", "no", ""):
        out = plugin.coerce_settings({"notify_enabled": falsy})
        assert out["notify_enabled"] is False, falsy


def test_coerce_settings_notify_enabled_string_true_is_true(plugin):
    for truthy in ("true", "True", "TRUE", "1", "yes"):
        out = plugin.coerce_settings({"notify_enabled": truthy})
        assert out["notify_enabled"] is True, truthy


def test_coerce_settings_boolean_fields_are_always_a_real_bool_type(plugin):
    """Not just truthy/falsy -- `is True`/`is False` must hold, since a later
    task's gate does `if not settings.get("notify_enabled"): return False`
    and a non-bool truthy/falsy value would still work by luck. Pin the type
    itself so a future refactor can't quietly reintroduce a stringly-typed
    value that merely happens to behave right today."""
    out = plugin.coerce_settings({"notify_enabled": "true",
                                  "exclude_auto_created": "false"})
    assert type(out["notify_enabled"]) is bool
    assert type(out["exclude_auto_created"]) is bool


def test_fields_include_every_setting_the_code_reads(plugin):
    ids = {f["id"] for f in plugin.FIELDS}
    required = {"poll_interval_s", "min_watch_seconds", "client_gap_grace_s",
                "merge_gap_s", "top_n", "never_watched_ceiling",
                "unused_threshold_days", "exclude_auto_created", "exclude_groups",
                "exclude_name_regex", "notify_enabled", "report_base_url",
                "report_schedule"}
    assert required <= ids


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


def test_validate_settings_rejects_report_base_url_without_scheme(plugin):
    result = plugin.Plugin().run("validate_settings", {},
                                 {"settings": {"report_base_url": "192.168.1.53:9191"}})
    assert result["status"] == "error"
    assert "report base url" in result["message"].lower()
    assert "http" in result["message"].lower()


def test_validate_settings_accepts_report_base_url_with_http_scheme(plugin):
    result = plugin.Plugin().run("validate_settings", {},
                                 {"settings": {"report_base_url": "http://192.168.1.53:9191"}})
    assert result["status"] == "ok"


def test_validate_settings_accepts_report_base_url_with_https_scheme(plugin):
    result = plugin.Plugin().run("validate_settings", {},
                                 {"settings": {"report_base_url": "https://example.com"}})
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


def test_show_summary_uses_the_gateway_clock_not_wall_clock(plugin, tmp_path,
                                                             monkeypatch):
    """M8 (Task 10): _show_summary must read `now` from _gateway().now(), the
    same clock _build_report uses -- not time.time() directly -- so the two
    actions can never disagree about "today" if the gateway's clock is ever
    faked (tests) or diverges from wall-clock (production). Uses a sentinel
    `now` far from real wall-clock time so a regression to time.time() would
    make the computed tracking-days figure nonsensical instead of ~10.0."""
    storage_mod = sys.modules["metricsarr_under_test.storage"]
    store = storage_mod.Storage(str(tmp_path / "data"))
    store.write({"channels": {},
                "meta": {"stats_since": NOW - 10 * 86400, "coverage": {}}}, NOW)

    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "_gateway", lambda: type(
        "FakeGW", (), {"now": lambda self: NOW})())

    result = plugin.Plugin()._show_summary({})
    assert "10.0 days" in result["message"]


def test_actions_expose_the_expected_ids(plugin):
    ids = {a["id"] for a in plugin.ACTIONS}
    assert {"build_report", "show_summary",
            "validate_settings"} <= ids


# ---- C1: build_report_task must actually register with Celery --------------

def test_build_report_task_is_registered_with_celery(plugin):
    celery_mod = sys.modules["celery"]
    task = plugin.build_report_task
    assert task.name in celery_mod.registered_tasks
    assert celery_mod.registered_tasks[task.name] is task


def test_build_report_task_runs_and_returns_counts(plugin, tmp_path, monkeypatch):
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


# ---- bug-078: a green result must never outlive a failed publish -----------
#
# The report's counts are computed BEFORE the write, and write_report never
# raises (it degrades, by design). So when /data/logos/metricsarr was owned by
# root and the Celery worker runs as `dispatch`, the scheduled report returned
# SUCCESS with a full set of counts while publishing NOTHING -- the failure was
# visible only by noticing report.html's mtime never moved. Same shape as the
# Task-10 leftover above (a return value is a return value), one layer down:
# here nothing raises in the first place.

def _fail_html_writes(plugin, monkeypatch, tmp_path, only_csv=False):
    """Point the plugin at tmp dirs and make the real _atomic_write fail the
    way an unwritable root-owned directory does (PermissionError IS an OSError,
    so this takes write_report's genuine degradation path)."""
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", _fake_gateway_with_no_channels)

    real = plugin.reports._atomic_write

    def guarded(path, text):
        if path.endswith(".csv") == only_csv:
            raise PermissionError(13, "Permission denied", path)
        return real(path, text)

    monkeypatch.setattr(plugin.reports, "_atomic_write", guarded)


def test_build_report_action_reports_error_when_the_html_write_fails(
        plugin, tmp_path, monkeypatch):
    _fail_html_writes(plugin, monkeypatch, tmp_path)
    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["status"] == "error"
    assert "Permission denied" in result["message"]


def test_build_report_task_raises_when_the_html_write_fails(
        plugin, tmp_path, monkeypatch):
    _fail_html_writes(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_load_settings", lambda: {})
    with pytest.raises(RuntimeError) as exc_info:
        plugin.build_report_task()
    assert "Permission denied" in str(exc_info.value)


def test_build_report_still_succeeds_when_only_the_csv_write_fails(
        plugin, tmp_path, monkeypatch):
    """The HTML report served by nginx is the product; the CSV is a convenience
    export to a bind mount. A CSV-only failure must stay a degraded success --
    fixing bug-078 must not turn that deliberate degradation into a hard fail."""
    _fail_html_writes(plugin, monkeypatch, tmp_path, only_csv=True)
    monkeypatch.setattr(plugin, "_load_settings", lambda: {})

    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["status"] == "ok"
    assert "csv write failed" in result["message"]
    assert plugin.build_report_task()["never_watched"] is not None


def test_build_report_task_logs_redacted_and_reraises_so_celery_can_retry(
        plugin, monkeypatch, caplog):
    """Task-10 leftover: returning {"error": True} on failure made Celery's
    result backend record the run as SUCCESS (a return value is a return
    value, whatever its contents), so a failed scheduled report could never
    be retried. The fix logs the redacted error and RE-RAISES it -- Celery
    sees a real failure -- while still never leaking credentials into the
    log or the exception text.

    I6: `str(exc_info.value)` alone gives FALSE ASSURANCE -- it only ever
    saw the message. `raise RuntimeError(redacted) from exc` sets
    __cause__ to the ORIGINAL exception, so Celery's stored/logged
    traceback renders the credential-bearing original verbatim even though
    the message string is clean. Assert over the FULL formatted traceback,
    the thing Celery actually stores/logs."""
    def boom():
        raise RuntimeError("http://host/live/topsecretuser/topsecretpass/x")

    monkeypatch.setattr(plugin, "_load_settings", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc_info:
            plugin.build_report_task()
    assert "topsecretpass" not in caplog.text
    assert "topsecretpass" not in str(exc_info.value)
    full_traceback = "".join(traceback.format_exception(exc_info.value))
    assert "topsecretpass" not in full_traceback
    assert exc_info.value.__cause__ is None


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


# ---- C1: the collector must actually START -- nothing else ever calls run() -

def test_plugin_init_spawns_the_collector_under_the_uwsgi_gate(plugin, monkeypatch):
    """C1: Plugin.run() is the ONLY thing that called ensure_collector() --
    but nothing calls run() until an action or settings-save fires, which
    Dispatcharr's settings-save flow never does. A user who installs,
    configures, and walks away collects NOTHING, forever. The platform hook
    is Plugin.__init__ (apps/plugins/apps.py's ready() calls
    discover_plugins() in every uWSGI worker, loader.py instantiates
    plugin_cls()) -- so __init__ must call ensure_collector() itself."""
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    spawned = []

    def fake_spawn(settings):
        thread = _make_thread(plugin)
        spawned.append(thread)
        return thread

    monkeypatch.setattr(plugin, "_spawn_collector", fake_spawn)
    try:
        plugin.Plugin()
        assert len(spawned) == 1
    finally:
        for thread in spawned:
            thread.metricsarr_stop.set()
            thread.join(timeout=2)


def test_plugin_init_does_not_spawn_the_collector_outside_uwsgi(plugin, monkeypatch):
    """The Celery/manage.py-shell/test process must never spawn a collector
    thread of its own -- only an actual uWSGI worker."""
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: False)
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    plugin.Plugin()
    assert spawned == []


def test_plugin_init_never_raises_even_if_ensure_collector_blows_up(plugin,
                                                                     monkeypatch):
    def boom(settings=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "ensure_collector", boom)
    plugin.Plugin()          # must not raise


# ---- I1: a running collector must pick up a settings change ----------------

def test_ensure_collector_respawns_on_a_settings_change_even_at_the_same_version(
        plugin, monkeypatch):
    """I1: thresholds are frozen into the collector loop's closure at spawn
    time. A live thread on the CURRENT version but STALE settings (e.g. the
    user lowered poll_interval_s) must be superseded exactly like a version
    bump -- otherwise the collector keeps polling at the OLD cadence forever
    while gates.coverage_fraction computes `needed` from the NEW configured
    interval, permanently zeroing coverage and poisoning every future report
    with a false "the collector was blind" banner."""
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    old = _make_thread(plugin)                 # fingerprint of default settings {}
    spawned = []

    def fake_spawn(settings):
        thread = _make_thread(plugin)
        spawned.append(thread)
        return thread

    monkeypatch.setattr(plugin, "_spawn_collector", fake_spawn)
    try:
        plugin.ensure_collector({"poll_interval_s": 5})
        assert old.metricsarr_stop.is_set()
        assert len(spawned) == 1
    finally:
        old.metricsarr_stop.set()
        old.join(timeout=2)
        for thread in spawned:
            thread.metricsarr_stop.set()
            thread.join(timeout=2)


def test_ensure_collector_does_not_respawn_when_settings_are_unchanged(plugin,
                                                                       monkeypatch):
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    old = _make_thread(plugin)
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    try:
        plugin.ensure_collector({})
        assert not old.metricsarr_stop.is_set()
        assert spawned == []
    finally:
        old.metricsarr_stop.set()
        old.join(timeout=2)


def test_ensure_collector_settings_change_respawn_shares_the_crash_loop_budget(
        plugin, monkeypatch):
    """A settings-change respawn is a supersession just like a version bump,
    so it must consume the SAME crash-loop budget (_restart_times) -- a burst
    of rapid settings edits must not bypass the thrash guard a burst of
    version bumps would hit."""
    monkeypatch.setattr(plugin, "_is_uwsgi_worker", lambda: True)
    old = _make_thread(plugin)
    monkeypatch.setattr(plugin, "_spawn_collector",
                        lambda settings: spawned.append(1))
    spawned = []
    now = time.time()
    monkeypatch.setattr(plugin, "_restart_times", [now] * plugin.RESTART_BOUND)
    try:
        plugin.ensure_collector({"poll_interval_s": 5})
        # crash-loop bound already saturated -- refuse the respawn, keep the
        # incumbent alive, exactly as it would for a version-bump respawn.
        assert spawned == []
        assert not old.metricsarr_stop.is_set()
        assert old.is_alive()
    finally:
        old.metricsarr_stop.set()
        old.join(timeout=2)


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


# ---- I2: _validate must redact its own messages -----------------------------
#
# NOTE: _validate used to call sync_schedule() itself and redact ITS failure
# too (test_validate_settings_redacts_scheduling_failure_message, removed) --
# dropped once sync_schedule was made run()'s single arming site (see the
# I3 tests below), since _validate calling it again just armed the schedule
# twice per click for no benefit and nothing about its failure is surfaced
# from _validate anymore (run()'s own call site silently swallows it, I3).

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


# ---- I4 (this fix wave): report_base_url turns the report link real -------
#
# Not to be confused with the "I4" label above (select-value validation) --
# that was a prior fix wave's finding, kept for its own history.

def test_report_base_url_field_exists_and_defaults_to_empty(plugin):
    field = next(f for f in plugin.FIELDS if f["id"] == "report_base_url")
    assert field["default"] == ""
    assert field["type"] == "string"


def _fake_gateway_with_no_channels():
    class FakeGateway:
        def now(self):
            return NOW

        def channels(self):
            return []

    return FakeGateway()


def test_build_report_message_uses_the_bare_path_when_base_url_is_unset(
        plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", _fake_gateway_with_no_channels)

    result = plugin.Plugin().run("build_report", {}, {"settings": {}})
    assert result["message"].endswith("Report: " + plugin.reports.REPORT_URL_PATH)


def test_build_report_message_uses_the_full_url_when_base_is_set(plugin, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", _fake_gateway_with_no_channels)

    result = plugin.Plugin().run(
        "build_report", {},
        {"settings": {"report_base_url": "http://192.168.1.53:9191"}})
    expected = "http://192.168.1.53:9191" + plugin.reports.REPORT_URL_PATH
    assert expected in result["message"]


# ---- I5: the collector loop has NO observability on an escaping exception --

def test_collector_loop_logs_on_a_persistent_failure(plugin, monkeypatch, caplog):
    """I5: run_tick's own try/except only sets stats["last_error"] for errors
    IT catches internally -- an exception escaping run_tick entirely (e.g.
    Collector construction itself failing because Redis is unreachable) had
    NO log line, no stats update, nothing. A persistently-broken collector
    was completely invisible. Force a persistent failure and prove it logs --
    the only observability this thread has."""
    def boom(*a, **k):
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(plugin.collector_mod, "Collector", boom)
    monkeypatch.setattr(plugin, "_get_redis", lambda: object())

    thread = None
    with caplog.at_level(logging.ERROR):
        thread = plugin._spawn_collector({"poll_interval_s": 5})
        time.sleep(0.3)
        thread.metricsarr_stop.set()
        thread.join(timeout=3)

    assert "collector tick failed" in caplog.text


# ---- I7 is covered in tests/test_no_mutations.py, not here ------------------


# ---- Task 7: wire the Newsflasharr emits into build_report_task ------------
#
# _emit_notifications is called from build_report_task ONLY -- the interactive
# build_report action deliberately does not emit (a single writer for the
# honesty-gate state, one report per scheduled run rather than one per click).
# It must never raise, must respect the notify_enabled toggle, and must only
# ever run after a CONFIRMED publish (bug-078's lesson one layer up: a report
# that failed to write must not still fire a notification about it).

class _FakeNotifyClient:
    """Stand-in for the vendored notify_client module -- exposes just the one
    attribute (`notify`) that _notify_client()'s caller reaches for."""

    def __init__(self, calls, raise_exc=None):
        self._calls = calls
        self._raise_exc = raise_exc

    def notify(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        self._calls.append(kwargs)
        return True


def _minimal_model(ok, tracked_days, alerts=None, coverage=0.95,
                   total_channels=1, immature=False):
    """A hand-built report `model` dict carrying every key render_html/
    write_report/summary_for_notify touch, so reports.build_model can be
    monkeypatched wholesale to control gate.ok/tracked_days directly instead
    of reverse-engineering real usage.json + ORM rows to hit gates.evaluate's
    exact branches.

    `immature` mirrors gates.evaluate()'s F1 field directly (default False:
    every call site here uses tracked_days=45, a mature dataset) -- kept as
    an explicit gate key rather than derived from tracked_days so this
    helper can't reintroduce the rounding seam notify_report.sensor_blind
    used to have.
    """
    return {
        "generated_at": NOW,
        "generated_at_local": "2023-11-14 00:00 UTC",
        "stats_since": NOW - tracked_days * 86400,
        "tracked_days": tracked_days,
        "coverage": coverage,
        "total_channels": total_channels,
        "never_watched": [],
        "tuned_never_qualified": [],
        "most_used": [],
        "least_used": [],
        "excluded": [],
        "unobservable": [],
        "group_rollup": [],
        "gate": {"ok": ok, "alerts": list(alerts or []), "coverage": coverage,
                 "immature": immature},
        "counts": {"never_watched": 0, "too_new": 0, "tuned_never_qualified": 0,
                  "watched": 0, "excluded": 0, "unobservable": 0},
    }


def _patch_report_dirs(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(plugin, "REPORT_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(plugin, "CSV_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(plugin, "_gateway", _fake_gateway_with_no_channels)


def test_toggle_off_emits_nothing(plugin, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient(calls))
    _patch_report_dirs(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_load_settings",
                        lambda: {"notify_enabled": False})

    plugin.build_report_task()
    assert calls == []


def test_toggle_on_emits_report_and_ledgerable_event(plugin, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient(calls))
    _patch_report_dirs(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(
        plugin, "_load_settings",
        lambda: {"notify_enabled": True,
                 "report_base_url": "http://192.168.1.53:9191"})

    plugin.build_report_task()
    assert len(calls) == 1
    kw = calls[0]
    assert kw["source"] == "metricsarr"
    assert kw["event"] == "usage_report"
    assert kw["attachment"]                     # written["archive_path"]
    assert kw["url"].startswith("http://192.168.1.53:9191")


def test_a_raising_notify_never_fails_the_task(plugin, monkeypatch):
    """`_notify_client()` itself raising -- e.g. Newsflasharr's vendored
    notify_client.py missing entirely, per this function's own docstring
    ("or Newsflasharr not being installed at all") -- reaches
    _emit_notifications's own try/except directly, unlike a raising
    notify_fn (already absorbed by notify_report.emit_report/emit_gate's own
    internal try/except, proven by test_notify_report.py). This is the one
    that actually exercises _emit_notifications's own wrapper."""
    def boom():
        raise ImportError("no module named notify_client")

    monkeypatch.setattr(plugin, "_notify_client", boom)
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html",
              "html_path": "/tmp/report.html"}

    # Must not raise -- a notify failure must never fail the report task.
    plugin._emit_notifications({"notify_enabled": True}, model, written)


def test_a_raising_notify_fn_never_fails_the_task_either(plugin, monkeypatch):
    """Defense in depth, one layer further out: even though
    notify_report.emit_report/emit_gate already swallow a raising notify_fn
    internally, the integration through _emit_notifications must still never
    surface it -- pins the contract at the boundary this task actually wires
    together, not just within notify_report's own unit tests."""
    monkeypatch.setattr(
        plugin, "_notify_client",
        lambda: _FakeNotifyClient([], raise_exc=RuntimeError(
            "http://host/live/notifysecretuser/notifysecretpass/x")))
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html",
              "html_path": "/tmp/report.html"}

    plugin._emit_notifications({"notify_enabled": True}, model, written)


def test_the_interactive_action_does_not_emit(plugin, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient(calls))
    _patch_report_dirs(plugin, monkeypatch, tmp_path)

    result = plugin.Plugin().run(
        "build_report", {}, {"settings": {"notify_enabled": True}})
    assert result["status"] == "ok"
    assert calls == []


def test_gate_state_round_trips_through_the_task(plugin, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient(calls))
    _patch_report_dirs(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_load_settings",
                        lambda: {"notify_enabled": True})
    # DATA_DIR already exists in production by the time a scheduled report
    # runs (the collector's very first tick creates it via Storage.write) --
    # replicate that here so this test exercises the real round-trip rather
    # than notify_state.json's own missing-directory degrade path (that
    # degrade is covered separately by test_notify_report.py).
    os.makedirs(str(tmp_path / "data"), exist_ok=True)

    blind = _minimal_model(ok=False, tracked_days=45, alerts=["sensor blind"])
    healthy = _minimal_model(ok=True, tracked_days=45)
    models = iter([blind, healthy])
    monkeypatch.setattr(plugin.reports, "build_model",
                        lambda *a, **k: next(models))

    state_path = os.path.join(str(tmp_path / "data"),
                              plugin.notify_report.STATE_FILE)

    plugin.build_report_task()
    assert plugin.notify_report.load_prev_ok(state_path) is False
    assert any(c.get("event") == "honesty_gate" and c.get("severity") == "critical"
              for c in calls)

    calls.clear()
    plugin.build_report_task()
    assert plugin.notify_report.load_prev_ok(state_path) is True
    assert any(c.get("kind") == "resolve" for c in calls)


def test_no_emit_when_the_publish_fails(plugin, tmp_path, monkeypatch):
    """Mutation-proof (c): the emit call must sit AFTER bug-078's publish
    check, never before it. If it moved earlier, this would start firing
    notifications about reports that were never written."""
    calls = []
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient(calls))
    _fail_html_writes(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_load_settings",
                        lambda: {"notify_enabled": True})

    with pytest.raises(RuntimeError):
        plugin.build_report_task()
    assert calls == []


# -- _emit_notifications must report what it did ------------------------------
# It discarded emit_report's bool, so a refused spool write (notify() returns
# False, it never raises) left the scheduled run reporting green with no trace.
# The new on-demand button cannot be honest without this signal either.

def test_emit_notifications_reports_a_successful_emit(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient([]))
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html", "html_path": "/tmp/report.html"}
    out = plugin._emit_notifications({"notify_enabled": True}, model, written)
    assert out["enabled"] is True
    assert out["report_emitted"] is True
    assert out["error"] is None


def test_emit_notifications_distinguishes_refused_from_successful(plugin, monkeypatch):
    """DIFFERENTIAL on purpose. Asserting only `report_emitted is False` for a
    refusal is HOLLOW -- False is ALSO the initialised default, so deleting the
    assignment entirely still passes it (proven by mutation). Only the contrast
    between the two outcomes pins that the bool is genuinely read."""
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html", "html_path": "/tmp/report.html"}

    class _Refuse:
        @staticmethod
        def notify(**kw):
            return False                    # spool refused; notify never raises

    monkeypatch.setattr(plugin, "_notify_client", lambda: _Refuse)
    refused = plugin._emit_notifications({"notify_enabled": True}, model, written)

    monkeypatch.setattr(plugin, "_notify_client", lambda: _FakeNotifyClient([]))
    accepted = plugin._emit_notifications({"notify_enabled": True}, model, written)

    assert refused["report_emitted"] is False
    assert accepted["report_emitted"] is True
    assert refused["report_emitted"] != accepted["report_emitted"]


def test_emit_notifications_reports_that_notifications_are_off(plugin):
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html", "html_path": "/tmp/report.html"}
    out = plugin._emit_notifications({"notify_enabled": False}, model, written)
    assert out["enabled"] is False
    assert out["report_emitted"] is False


def test_emit_notifications_reports_an_import_failure(plugin, monkeypatch):
    def boom():
        raise ImportError("no module named notify_client")
    monkeypatch.setattr(plugin, "_notify_client", boom)
    model = _minimal_model(ok=True, tracked_days=45)
    written = {"archive_path": "/tmp/report-1.html", "html_path": "/tmp/report.html"}
    out = plugin._emit_notifications({"notify_enabled": True}, model, written)
    assert out["report_emitted"] is False
    assert out["error"]                      # named, not silent
