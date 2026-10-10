"""Dustarr reports its Reports Built total through the vendored usage client.

The counter is the integer field reports_built of /data/dustarr/report_count.json.
bump_report_count writes it, and reports, forced, only after the new value is on disk:
that is the one forced report site, reached by both the action and the scheduled Celery
task through _build_report. run() reports once more for each of its four actions, whatever
the result, through a non-forced call. The checkbox sits in its own section at the end of
the form.
"""
import json
import pathlib

import pytest

from conftest import load_plugin


@pytest.fixture()
def mod():
    return load_plugin()


class FakeUsage:
    def __init__(self, on_report=None):
        self.calls = []
        self.on_report = on_report

    def report(self, settings=None, logger=None, force=False):
        if self.on_report:
            self.on_report()
        self.calls.append((settings, force))


@pytest.fixture()
def usage(mod, monkeypatch):
    fake = FakeUsage()
    monkeypatch.setattr(mod, "USAGE", fake)
    return fake


@pytest.fixture()
def counter_dir(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return tmp_path


def _plugin(mod):
    return mod.Plugin.__new__(mod.Plugin)


def test_the_reporter_is_configured_for_dustarr(mod):
    u = mod.USAGE
    assert (u.plugin, u.counter, u.label) == ("dustarr", "reports_built", "Reports Built")


def test_the_total_reads_the_counter_file(mod, counter_dir):
    (counter_dir / "report_count.json").write_text(json.dumps({"reports_built": 7}), encoding="utf-8")
    assert mod.USAGE.total_fn() == 7


@pytest.mark.parametrize("content", [
    None,
    "{not json",
    json.dumps({"reports_built": True}),
    json.dumps({"reports_built": -3}),
    json.dumps({"reports_built": "4"}),
])
def test_a_missing_or_bad_counter_totals_zero(mod, counter_dir, content):
    if content is not None:
        (counter_dir / "report_count.json").write_text(content, encoding="utf-8")
    assert mod.USAGE.total_fn() == 0


def test_settings_are_read_under_the_dustarr_key(mod, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "load_plugin_settings", lambda key, logger=None: seen.append(key) or {})
    mod.USAGE.settings_fn()
    assert seen == ["dustarr"]


def test_a_published_report_is_forced_after_the_counter_moves(mod, counter_dir, monkeypatch):
    seen = []
    fake = FakeUsage(on_report=lambda: seen.append(mod.read_report_count()))
    monkeypatch.setattr(mod, "USAGE", fake)
    assert mod.bump_report_count() == 1
    assert fake.calls == [(None, True)]
    assert seen == [1]


def test_a_report_that_was_not_published_neither_counts_nor_reports(mod, counter_dir, usage, monkeypatch):
    blocker = counter_dir / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "DATA_DIR", str(blocker / "sub"))
    assert mod.bump_report_count() == 0
    assert usage.calls == []


def test_a_failing_reporter_never_breaks_the_count(mod, counter_dir, monkeypatch):
    class Exploding:
        def report(self, settings=None, logger=None, force=False):
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "USAGE", Exploding())
    assert mod.bump_report_count() == 1
    assert mod.read_report_count() == 1


@pytest.mark.parametrize("action", ["build_report", "show_summary", "validate_settings", "report_issue"])
def test_a_known_action_reports_once_with_the_live_settings(mod, usage, monkeypatch, action):
    p = _plugin(mod)
    result = {"status": "ok", "message": "ok"}
    monkeypatch.setattr(p, "_run_action", lambda a, params, context: result)
    settings = {"notify_enabled": False}
    assert p.run(action, None, {"settings": settings}) is result
    assert usage.calls == [(settings, False)]


def test_an_error_result_still_reports(mod, usage, monkeypatch):
    p = _plugin(mod)
    err = {"status": "error", "error": "no", "message": "no"}
    monkeypatch.setattr(p, "_run_action", lambda a, params, context: err)
    assert p.run("validate_settings", None, {"settings": {}}) is err
    assert usage.calls == [({}, False)]


def test_an_action_that_raises_still_reports(mod, usage, monkeypatch):
    p = _plugin(mod)

    def boom(a, params, context):
        raise RuntimeError("boom")

    monkeypatch.setattr(p, "_run_action", boom)
    with pytest.raises(RuntimeError):
        p.run("show_summary", None, {"settings": {"x": 1}})
    assert usage.calls == [({"x": 1}, False)]


def test_an_unknown_action_does_not_report(mod, usage, monkeypatch):
    p = _plugin(mod)
    monkeypatch.setattr(p, "_run_action", lambda a, params, context: {"status": "error", "error": "x", "message": "x"})
    p.run("no_such_action", None, {"settings": {}})
    assert usage.calls == []


def test_a_context_without_settings_passes_none(mod, usage, monkeypatch):
    p = _plugin(mod)
    monkeypatch.setattr(p, "_run_action", lambda a, params, context: {"status": "ok", "message": "ok"})
    p.run("validate_settings", None, {})
    assert usage.calls == [(None, False)]


def test_the_reported_actions_are_exactly_the_declared_actions(mod, usage, monkeypatch):
    p = _plugin(mod)
    monkeypatch.setattr(p, "_run_action", lambda a, params, context: {"status": "ok", "message": "ok"})
    declared = {a["id"] for a in mod.ACTIONS}
    reported = set()
    for action in declared | {"no_such_action"}:
        before = len(usage.calls)
        p.run(action, None, {"settings": {}})
        if len(usage.calls) > before:
            reported.add(action)
    # ACTIONS holds four ids (validate_settings, show_summary, build_report,
    # report_issue). report_issue does no plugin work and is counted by design.
    assert reported == declared


def test_a_published_build_reports_forced_then_unforced(mod, counter_dir, usage, monkeypatch):
    p = _plugin(mod)
    result = {"status": "ok", "message": "ok", "file": "/x.html"}
    written = {"html_path": "/x.html"}

    def fake_build(settings, is_scheduled=False):
        mod.bump_report_count()          # the real writer; it reports forced
        return result, {}, written       # _build_report returns (result, model, written)

    monkeypatch.setattr(mod, "_build_report", fake_build)
    s = {"notify_enabled": False}
    p.run("build_report", None, {"settings": s})
    assert usage.calls == [(None, True), (s, False)]


def test_the_conftest_guard_intercepts_a_real_send(mod, monkeypatch, _no_real_usage_reporting):
    # Positive control: the guard must catch a real send path, or it proves nothing.
    monkeypatch.setattr(mod.USAGE, "_lock", lambda fd: True)
    monkeypatch.setattr(mod.USAGE, "settings_fn", lambda: {"share_usage_counts": True})
    mod.USAGE.report({"share_usage_counts": True}, None)
    assert len(_no_real_usage_reporting) == 1


def test_the_form_ends_with_the_usage_section_and_checkbox(mod):
    fields = _plugin(mod).fields
    assert fields[-1] == mod.with_usage_field([], mod.USAGE)[0]
    assert fields[-1]["id"] == "share_usage_counts"
    assert fields[-2]["id"] == "_section_usage" and fields[-2]["type"] == "info"
    assert [f.get("id") for f in fields].count("share_usage_counts") == 1


def test_the_docs_name_the_usage_count_as_the_only_network_request():
    """Three sentences once said the plugin makes no network request. They are now
    corrected; this keeps them from coming back while the usage count is shipped."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    banned = ("No internet access of any kind", "makes no outbound network request")
    for name in ("README.md", "SECURITY.md"):
        text = (repo / name).read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{name} still says the plugin makes no network request"
    security = (repo / "SECURITY.md").read_text(encoding="utf-8")
    assert "plugin-stats.dpas.workers.dev" in security
