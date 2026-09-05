"""The preamble at the top of the CSV export, which is what a reader actually meets first.

Measured 2026-09-05: this plugin's CSV had NO preamble at all. `render_csv`
wrote the column header row and then the data, so a file named report-<stamp>.csv
sitting in a folder gave a reader no clue what it was, which run produced it,
what that run found, or what settings it was judged against. The sibling plugin
Stream-Mapparr had about 45 preamble lines and the fault there was that two of
them were false; here there was nothing to be false.

That absence also meant this plugin could not have the specific defect found in
Stream-Mapparr, where a preamble line read a settings key the plugin had never
declared and therefore printed "(none)" on every export ever written. These
tests make that defect impossible going forward rather than merely absent: the
label map is bound to the field list the plugin actually declares, so a key that
does not exist fails the build.

`reports.py` must not import `plugin.py`, so the setting labels are duplicated
in `reports.py` and bound back by test, which is the same arrangement this
plugin already uses for ISSUES_URL and REPO_URL.
"""
import io
import os

import pytest
from conftest import NOW, load_plugin, model

load_plugin()


@pytest.fixture()
def plugin():
    module = load_plugin()
    module._restart_times.clear()
    return module


def _model(rp, gw, **overrides):
    m = model(rp, gw)
    m.setdefault("thresholds", {})
    m.update(overrides)
    return m


def _line(text, prefix):
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting {prefix!r} in:\n{text}")


def _has(text, prefix):
    return any(line.startswith(prefix) for line in text.splitlines())


# --------------------------------------------------------------------------- #
# Every setting label the preamble prints must actually exist
# --------------------------------------------------------------------------- #
def test_every_label_the_preamble_prints_is_a_setting_the_plugin_declares(rp, plugin):
    """The Stream-Mapparr defect: a preamble line read a key that never existed,
    so it printed nothing useful on every export and no test noticed."""
    declared = {f["id"] for f in plugin.Plugin.__new__(plugin.Plugin).fields}
    unknown = [key for key in rp.SETTING_LABELS if key not in declared]
    assert unknown == [], f"preamble reads settings that do not exist: {unknown}"


def test_each_label_matches_the_one_the_settings_form_shows(rp, plugin):
    """A reader looks the setting up in the interface by its label. A stale
    duplicate here sends them looking for a control that is not there."""
    form = {f["id"]: f.get("label")
            for f in plugin.Plugin.__new__(plugin.Plugin).fields}
    wrong = {key: (label, form.get(key))
             for key, label in rp.SETTING_LABELS.items()
             if form.get(key) != label}
    assert wrong == {}, f"preamble label does not match the form: {wrong}"


def test_the_preamble_covers_every_stored_setting(rp, plugin):
    """A setting added later must be recorded too, or the file stops being a
    complete record of the run that produced it."""
    stored = set(plugin.coerce_settings({}))
    missing = stored - set(rp.SETTING_LABELS)
    assert missing == set(), f"settings absent from the preamble: {missing}"


# --------------------------------------------------------------------------- #
# What the file is
# --------------------------------------------------------------------------- #
def test_the_file_says_what_it_is_before_it_says_how_it_was_configured(rp, gw):
    text = rp.render_csv(_model(rp, gw))
    top = " ".join(text.splitlines()[:12]).lower()
    assert "dustarr" in top
    assert "watched" in top, "the top of the file never says what it is about"


def test_the_top_of_the_file_explains_that_the_hash_lines_are_not_data(rp, gw):
    """A reader importing this into a spreadsheet has to be told to skip them."""
    top = " ".join(rp.render_csv(_model(rp, gw)).splitlines()[:12]).lower()
    assert "#" in top
    assert any(word in top for word in ("skip", "ignore", "not data")), top


def test_every_preamble_line_is_commented(rp, gw):
    """One uncommented line would be read as data by a spreadsheet import."""
    text = rp.render_csv(_model(rp, gw))
    preamble = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        preamble.append(line)
    assert preamble, "there is no preamble at all"
    assert all(line.startswith("#") for line in preamble)


def test_the_column_header_row_is_the_first_line_that_is_not_a_comment(rp, gw):
    text = rp.render_csv(_model(rp, gw))
    first_data = next(line for line in text.splitlines()
                      if not line.startswith("#"))
    # The data columns keep their machine names; only the preamble is prose.
    assert first_data.startswith("name,group,"), first_data


def test_the_rows_are_unchanged_by_the_preamble(rp, gw):
    """The preamble is added above the existing output, not woven into it."""
    text = rp.render_csv(_model(rp, gw))
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(body) >= 2, "the data rows are gone"


# --------------------------------------------------------------------------- #
# What the run did, near the top
# --------------------------------------------------------------------------- #
def test_the_preamble_states_what_the_run_found(rp, gw):
    """Counts of what was found are what a reader wants first, not last."""
    text = rp.render_csv(_model(rp, gw))
    assert "3" in _line(text, "# Never watched:")
    assert "2" in _line(text, "# Watched at least once:")


def test_what_the_run_found_comes_before_the_settings_it_used(rp, gw):
    text = rp.render_csv(_model(rp, gw))
    lines = text.splitlines()
    found = next(i for i, line in enumerate(lines) if line.startswith("# Never watched:"))
    settings = next(i for i, line in enumerate(lines) if line.startswith("# Poll interval"))
    assert found < settings, "the settings are printed above the result"


def test_a_scheduled_run_does_not_describe_itself_as_manual(rp, gw):
    """The Stream-Mapparr defect: the mode was hardcoded inside an action that
    the scheduler also calls, so every scheduled run claimed to be manual."""
    text = rp.render_csv(_model(rp, gw, is_scheduled=True))
    assert "Scheduled" in _line(text, "# Run mode:")


def test_a_manual_run_says_manual(rp, gw):
    text = rp.render_csv(_model(rp, gw, is_scheduled=False))
    assert "Manual" in _line(text, "# Run mode:")


def test_the_scheduled_task_reports_that_it_was_scheduled(plugin, monkeypatch):
    """The value has to travel from the Celery task into the model, and a test
    on the preamble alone cannot see that journey."""
    seen = {}

    def spy(rows, usage, thresholds, now):
        seen["called"] = True
        return {"counts": {"never_watched": 0}, "gate": {"ok": True},
                "total_channels": 0, "tracked_days": 0, "coverage": 0.0}

    captured = {}

    def capture(model, report_dir, csv_dir, now, retention_days=0):
        captured["is_scheduled"] = model.get("is_scheduled")
        return {"html_path": None, "csv_path": None, "archive_path": None}

    monkeypatch.setattr(plugin.reports, "build_model", spy)
    monkeypatch.setattr(plugin.reports, "write_report", capture)
    monkeypatch.setattr(plugin, "_gateway", lambda: _StubGateway())

    plugin._build_report({}, is_scheduled=True)
    assert captured["is_scheduled"] is True

    plugin._build_report({}, is_scheduled=False)
    assert captured["is_scheduled"] is False


class _StubGateway:
    def now(self):
        return NOW

    def channels(self):
        return []


def test_the_celery_task_marks_its_run_as_scheduled(plugin):
    """Reads the source, because the flag has to be passed at the call site."""
    import ast
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "dustarr", "plugin.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "build_report_task")
    for call in ast.walk(func):
        if (isinstance(call, ast.Call)
                and ast.unparse(call.func).endswith("_build_report")):
            passed = {k.arg: ast.unparse(k.value) for k in call.keywords}
            assert passed.get("is_scheduled") == "True", passed
            return
    raise AssertionError("build_report_task does not build a report")


# --------------------------------------------------------------------------- #
# Plain wording in the settings record
# --------------------------------------------------------------------------- #
def test_settings_read_as_yes_and_no_rather_than_python_booleans(rp, gw):
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_auto_created": True, "notify_enabled": False}))
    assert "Yes" in _line(text, "# Exclude auto-created channels:")
    assert "No" in _line(text, "# Send notifications to Newsflasharr:")
    assert "True" not in text and "False" not in text, \
        "raw Python booleans still reach the reader"


def test_a_setting_stored_as_a_string_still_reads_as_yes_or_no(rp, gw):
    """Dispatcharr stores some booleans as the strings true and false."""
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_auto_created": "true", "notify_enabled": "false"}))
    assert "Yes" in _line(text, "# Exclude auto-created channels:")
    assert "No" in _line(text, "# Send notifications to Newsflasharr:")


def test_the_alarm_ceiling_says_what_the_number_means(rp, gw):
    """0.98 of what, and is higher stricter? The number alone answers neither."""
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "never_watched_ceiling": 0.98})), "# Never-watched alarm ceiling:")
    assert "%" in line
    assert "tolerant" in line.lower() or "strict" in line.lower(), line


def test_the_row_count_setting_says_what_it_counts(rp, gw):
    line = _line(rp.render_csv(_model(rp, gw, thresholds={"top_n": 20})),
                 "# Rows in the Most used and Least used tables:")
    assert "20" in line
    assert "row" in line.lower(), line


def test_the_retention_setting_says_what_zero_means(rp, gw):
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "report_retention_days": 0})), "# Delete saved reports older than (days):")
    assert "off" in line.lower(), line
    assert "kept" in line.lower(), line


def test_an_empty_setting_reads_as_none_rather_than_as_a_blank(rp, gw):
    line = _line(rp.render_csv(_model(rp, gw, thresholds={"exclude_groups": ""})),
                 "# Excluded groups (comma separated):")
    assert "none" in line.lower(), line


def test_a_setting_that_was_not_recorded_says_so_rather_than_inventing_a_value(rp, gw):
    """An absent value must not read as a real one. This is what told the
    Stream-Mapparr reader nothing was configured when something was."""
    text = rp.render_csv(_model(rp, gw, thresholds={}))
    assert "not recorded" in _line(text, "# Poll interval (s):").lower()


# --------------------------------------------------------------------------- #
# The file has to survive a spreadsheet
# --------------------------------------------------------------------------- #
def test_the_preamble_is_plain_ascii(rp, gw):
    """A CSV may be opened by a spreadsheet under a different codepage, where
    anything outside ASCII arrives as mojibake."""
    text = rp.render_csv(_model(rp, gw))
    preamble = "\n".join(line for line in text.splitlines()
                         if line.startswith("#"))
    bad = sorted({c for c in preamble if ord(c) > 127})
    assert not bad, [hex(ord(c)) for c in bad]


def test_the_preamble_uses_no_em_dashes_or_double_hyphens(rp, gw):
    text = rp.render_csv(_model(rp, gw))
    preamble = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    assert chr(0x2014) not in preamble
    assert "--" not in preamble


def test_the_preamble_never_raises_on_a_malformed_model(rp):
    """`write_report` catches only OSError, so anything else escapes to run().

    A model missing every key it reads must still produce a file.
    """
    text = rp.render_csv({"never_watched": [], "tuned_never_qualified": [],
                          "most_used": [], "least_used": [], "excluded": [],
                          "unobservable": [], "cold_abandoned": [],
                          "cold_still_tried": []})
    assert text.startswith("#")


def test_the_preamble_never_leaks_a_provider_credential(rp, gw):
    """Settings are rendered verbatim, and one of them is a regular expression
    the operator typed. Nothing here should carry a URL, but a value that does
    must not reach a file that gets emailed."""
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_name_regex": "http://user:pass@host/live/1.ts"}))
    line = _line(text, "# Excluded name regex:")
    assert "pass" not in line, line


# --------------------------------------------------------------------------- #
# Wording faults found by rendering the preamble and reading it
# --------------------------------------------------------------------------- #
def test_the_schedule_line_names_the_option_the_form_shows(rp, gw):
    """The stored value is the bare word `weekly`. The form offers
    `Weekly (Mon 03:00)`, and the time of day is the part a reader wants."""
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "report_schedule": "weekly"})), "# Scheduled report:")
    assert "03:00" in line, line
    assert "Mon" in line, line


def test_every_schedule_option_is_named_the_way_the_form_names_it(rp, plugin):
    """Bound to the form, so an option renamed there cannot leave a stale name
    here. This is the same defect class as a settings key that never existed."""
    form = next(f for f in plugin.Plugin.__new__(plugin.Plugin).fields
                if f["id"] == "report_schedule")
    declared = {o["value"]: o["label"] for o in form["options"]}
    assert rp.SCHEDULE_LABELS == declared


def test_an_unrecognised_schedule_value_is_printed_rather_than_hidden(rp, gw):
    """A stored value the form no longer offers must still be visible: it is
    what the run used, and silently dropping it hides the cause of a surprise."""
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "report_schedule": "fortnightly"})), "# Scheduled report:")
    assert "fortnightly" in line, line


def test_a_whole_number_setting_does_not_print_a_decimal_point(rp, gw):
    """coerce_settings returns floats for most numbers, so the record read
    `Minimum watch (s): 120.0`, which invites the reader to wonder what the
    fraction is for."""
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "min_watch_seconds": 120.0})), "# Minimum watch (s):")
    assert line.endswith("120"), line


def test_a_genuinely_fractional_setting_keeps_its_fraction(rp, gw):
    line = _line(rp.render_csv(_model(rp, gw, thresholds={
        "merge_gap_s": 12.5})), "# Session merge gap (s):")
    assert "12.5" in line, line


def test_the_row_count_line_does_not_repeat_its_own_label(rp, gw):
    """It read `Rows in the Most used and Least used tables: 20 (rows in each
    of the two ranking tables)`, which says the same thing twice."""
    line = _line(rp.render_csv(_model(rp, gw, thresholds={"top_n": 20})),
                 "# Rows in the Most used and Least used tables:")
    assert line.lower().count("row") == 1, line


# --------------------------------------------------------------------------- #
# The preamble's comment marker must not be able to swallow real data
# --------------------------------------------------------------------------- #
# Found by review on 2026-09-05, after the preamble above was added. The file
# now tells its reader to skip lines beginning with a hash, and the plugin's
# own tests/conftest.py::csv_data helper does exactly that. Nothing stopped a
# CHANNEL NAME from beginning with a hash, so a channel called "#1 Sports HD"
# became indistinguishable from a preamble line and was silently dropped by
# every comment-aware import, including this suite's own helper. Measured
# before the fix: an export of two such channels left ONLY the column header
# row after the strip.


def _named(rp, gw, *names):
    from conftest import SETTINGS
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=name, group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True)
            for i, name in enumerate(names, start=1)]
    usage = {"channels": {}, "meta": {"stats_since": NOW - 40 * 86400,
                                      "coverage": {}}}
    m = rp.build_model(rows, usage, SETTINGS, NOW)
    m["thresholds"] = {}
    return m


def test_a_channel_named_with_a_leading_hash_survives_a_comment_strip(rp, gw):
    """Two channels, not one: with a single channel a broken guard still leaves
    a plausible looking file, and the count assertion below would not move."""
    import csv

    from conftest import csv_data
    text = rp.render_csv(_named(rp, gw, "#1 Sports HD", "#2 News HD"))

    rows = list(csv.DictReader(csv_data(text)))
    assert len(rows) == 2, "a comment-aware import lost the hash-named channels"
    assert sorted(r["name"] for r in rows) == ["'#1 Sports HD", "'#2 News HD"]


def test_a_leading_hash_is_neutralized_the_same_way_a_formula_lead_is(rp, gw):
    """The existing mitigation is a leading single quote, which spreadsheets
    render as literal text. Reusing it keeps one mechanism, not two."""
    text = rp.render_csv(_named(rp, gw, "#1 Sports HD"))
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert any(line.startswith("'#1 Sports HD") for line in body), body


def test_an_ordinary_channel_name_is_still_left_alone(rp, gw):
    """The guard must not start quoting names that were never a problem."""
    text = rp.render_csv(_named(rp, gw, "BBC One HD"))
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert any(line.startswith("BBC One HD") for line in body), body


# --------------------------------------------------------------------------- #
# A line break inside a setting value must not escape the preamble
# --------------------------------------------------------------------------- #
# Also found by review on 2026-09-05. Settings arrive unvalidated from the API,
# and two of them are free text the operator typed. A newline inside one ended
# the comment block early: measured before the fix, a value of "FOO\nBAR" made
# the first non-comment line "BAR", so an importer read BAR as the column
# header row and the whole export became unreadable, with no error anywhere.


def test_a_newline_in_a_setting_value_does_not_end_the_preamble(rp, gw):
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_name_regex": "FOO" + chr(10) + "BAR"}))
    first = next(line for line in text.splitlines() if not line.startswith("#"))
    assert first.startswith("name,group,"), \
        f"the preamble leaked into the data: first data line was {first!r}"


def test_a_carriage_return_in_a_setting_value_does_not_end_the_preamble(rp, gw):
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_groups": "News" + chr(13) + "Sports"}))
    first = next(line for line in text.splitlines() if not line.startswith("#"))
    assert first.startswith("name,group,")


def test_a_unicode_line_separator_in_a_setting_value_does_not_end_the_preamble(rp, gw):
    """U+2028 is not a newline to a CSV reader, but str.splitlines DOES split on
    it, so a Python consumer stripping comment lines would break on it while a
    spreadsheet did not. Both readers have to see one line."""
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_name_regex": "FOO" + chr(0x2028) + "BAR"}))
    first = next(line for line in text.splitlines() if not line.startswith("#"))
    assert first.startswith("name,group,")


def test_the_whole_preamble_stays_one_comment_line_per_setting(rp, gw):
    """Every line of the preamble is commented however hostile a value is."""
    text = rp.render_csv(_model(rp, gw, thresholds={
        "exclude_name_regex": "A" + chr(10) + "B" + chr(13) + "C" + chr(0x2028) + "D",
        "exclude_groups": "X" + chr(10) + "Y"}))
    preamble = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        preamble.append(line)
    assert all(line.startswith("#") for line in preamble)
    assert any("A B C D" in line for line in preamble), \
        "the line breaks should be collapsed to spaces, not deleted"


# --------------------------------------------------------------------------- #
# The record must not contradict the run
# --------------------------------------------------------------------------- #
def test_the_yes_no_record_agrees_with_how_the_plugin_reads_the_same_value(rp, plugin):
    """Measured 2026-09-05: the two disagreed on "off", "disabled" and
    "enabled", where the plugin treats the toggle as ON and the record said No.

    Several values, not one: a single agreeing value proves nothing, because
    most strings already agreed.
    """
    disagreements = []
    for raw in ("true", "false", "1", "0", "yes", "no", "on", "off",
                "disabled", "enabled", "", "TRUE", "  True  ", True, False):
        coerced = plugin.coerce_settings({"notify_enabled": raw})["notify_enabled"]
        expected = "Yes" if coerced else "No"
        actual = rp._yes_no(raw)
        if actual != expected:
            disagreements.append((raw, expected, actual))
    assert disagreements == [], \
        f"the CSV record contradicts the plugin for: {disagreements}"


def test_a_line_break_in_the_schedule_value_also_stays_one_line(rp, gw):
    """`coerce_settings` restricts this select to its known options, so a break
    cannot arrive here through the normal path. The guard is still required:
    `_setting_line` renders whatever the model carries, and an unrecognised
    value is deliberately printed rather than hidden."""
    text = rp.render_csv(_model(rp, gw, thresholds={
        "report_schedule": "week" + chr(10) + "ly"}))
    first = next(line for line in text.splitlines() if not line.startswith("#"))
    assert first.startswith("name,group,")


def test_an_unset_row_count_reads_as_none_rather_than_as_the_word_None(rp, gw):
    """The row-count setting used to have a branch of its own that printed the
    literal "None". Removing it was not only tidying: the generic tail renders
    an absent or empty value as "(none)", which is what the other settings do.
    """
    for stored in (None, ""):
        line = _line(rp.render_csv(_model(rp, gw, thresholds={"top_n": stored})),
                     "# Rows in the Most used and Least used tables:")
        assert "(none)" in line, (stored, line)
        assert "None" not in line, (stored, line)
