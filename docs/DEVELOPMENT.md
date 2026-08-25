# Development

How this plugin is put together, and the constraints that are not obvious from
reading one file. For contribution mechanics, see
[CONTRIBUTING.md](../CONTRIBUTING.md).

## There is no way to run it standalone

The plugin runs inside Dispatcharr's Django backend. There is no build step, no
development server and no staging copy. The safety net is the test suite, which
stubs Django so every module imports in isolation.

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

## The modules

| Module | Role |
|---|---|
| `collector.py` | Samples Redis to see which channels have viewers. Runs in a background thread, one per worker, coordinated by a lease so only one does the work. |
| `sessionizer.py` | Turns samples into sessions. A watch is at least the minimum watch time; anything shorter is recorded separately as a tune. |
| `storage.py` | Reads and writes the viewing data file atomically. |
| `gates.py` | The honesty gates: dataset age, coverage, and the never-watched ceiling. |
| `gateway.py` | The only module that touches Dispatcharr's models. |
| `reports.py` | Builds the report model and renders the HTML page and the CSV export. |
| `notify_report.py`, `notify_client.py` | Sending the report through the Newsflasharr plugin. |
| `redaction.py` | Removes credential-shaped values from anything that gets logged or sent. |
| `plugin.py` | Settings, actions, the scheduled task, and arming the schedule. |

## Constraints with tests behind them

- **The plugin never writes to Dispatcharr.** `tests/test_no_mutations.py`
  walks the syntax tree of every shipped module and fails on a write-shaped
  database call. It decides by proving what the call is made on, so it allows
  writing to a dictionary or a set while refusing a write to a model, and it
  fails when it cannot prove the receiver. Read its docstring before adding any
  write.
- **`collector.py`, `sessionizer.py`, `storage.py`, `gates.py` and
  `redaction.py` use the standard library only.** The collector thread must
  never touch the database.
- **`gateway.py` and `reports.py` import Django inside functions, never at
  module level.** A module-level Django import breaks Dispatcharr's plugin
  loader.

## The invariant to hold on to

The list of channels comes from Dispatcharr. The viewing data file is a sparse
overlay containing only channels that have been watched. **A channel missing
from it has never been watched**, which is the expected state and the entire
point of the plugin, not an error to be defended against.

## Rendering the report

- **The page must work with no server.** It is opened from disk, emailed as an
  attachment and read on a television browser. Everything is inline: the styles,
  the charts as generated SVG, and the logo as an embedded data URI. No content
  delivery network, no web font, no linked image. A test forbids any external
  reference.
- **The renderer has no safety net.** The file writer catches file errors only,
  so any other exception means no report at all. Every helper a renderer calls
  has to be total over its inputs: not-a-number, both infinities, `None`, an
  unparseable string, a zero denominator. Each of the three chart generators
  needed a fix round for exactly this.
- **Colour comes from CSS rules, never from an SVG presentation attribute.**
  A `fill` attribute referring to a CSS variable is not reliably supported and
  fails silently to black, which is invisible against the dark surface.
- **The styles run on a token layer.** No literal colour inside a rule, because
  light and dark mode cannot both be right about one; spacing comes from the
  scale; text hierarchy comes from the three ink tokens rather than from
  opacity. Four tests enforce this, and each was proven by planting a
  regression.
- **The palette values are pinned by tests.** They were checked for colourblind
  safety against this page's own surfaces with a tool that does not live in this
  repository, so the numbers cannot be re-derived here. The pinning tests are
  the only guard.
- `tests/fixtures/sample_report.html` is a committed rendered report, regenerated
  with `python scripts/render_sample.py`. A rendering change fails that test by
  design: it is the prompt to look at the output, not a nuisance.

## The scheduled report

The scheduled report is a background job registered with Dispatcharr's
scheduler. Two things about it have caused real outages:

- **Dispatcharr registers a plugin's scheduled task on only one of its
  background workers.** The job has to be queued to that worker or it is
  accepted and never executed. The plugin re-asserts this every time an action
  runs, because the scheduling helper has no parameter for it and every action
  re-registers the job.
- **Stopping the plugin must not cancel the schedule.** Dispatcharr stops every
  plugin when the Refresh control on the Plugins page is pressed, not only on
  disable and uninstall, and nothing re-creates the schedule at load time. The
  stop handler therefore removes the schedule only when the plugin is being
  disabled or uninstalled, and keeps it in every other case, including when no
  reason is supplied.

**A run counter is not evidence a job executed.** The scheduler's counter counts
messages sent. The only thing that proves a scheduled report actually ran is the
timestamp the task itself writes after it has verified a report was published.
For that reason, do not run the scheduled task by hand: it would overwrite the
one signal that answers the question. Use the Build report action instead, which
does the same work without touching that timestamp.

## Publishing

Run the workspace publish audit before any push. A second, narrower scan runs
automatically in CI on every push to `master`
(`.github/workflows/publish-audit.yml`), applying a repository-specific deny
list. That deny list is the half that catches real problems, because a provider
hostname and a local network prefix look like ordinary words to a generic
scanner.

**The deny list is not in this repository, deliberately.** It spells out the
exact strings it exists to keep out. Each pattern is written with a single
character class so the rules do not match themselves while scanning, and that
defeats the scanner but not a person reading the file, so committing it would
publish everything it protects. The rules come from one of two places, and
`.github/scripts/audit_rules.py` is what loads them:

| Where | Used by | Notes |
|---|---|---|
| `PUBLISH_AUDIT_RULES` environment variable, holding the rules as JSON | CI | Supplied from the `PUBLISH_AUDIT_RULES` repository secret. |
| `.publish-audit.json` in the repository root | A maintainer machine, and the pre-commit hook | Gitignored, so a fresh clone does not have it and never will. |

`.publish-audit.example.json` is committed and documents the schema with
illustrative values only.

**A finding names the rule by its POSITION in the deny list, never by its
pattern.** Continuous integration logs follow repository visibility, so on a
public repository anything these scripts print is published, and a deny pattern
spells out the string it exists to keep out. A finding reads
`path/to/file.py:42 matched deny rule 2`, which is enough to act on with a local
copy of the rules in front of you. The matched text is not printed either: on a
real finding, that text is the secret. `tests/test_audit_scripts.py` plants a
match and asserts the pattern does not reach the output.

**If neither source is available the scripts exit non-zero rather than skipping
the scan.** That direction is deliberate: a check whose input is missing must
fail loudly, because a silent pass is indistinguishable from a genuinely clean
tree. If CI starts failing on a fresh fork or a new checkout, a missing secret
is the first thing to check.

**A clean scan and a broken rule look identical**, so plant a canary containing
each denied string at a path that is actually scanned and confirm every rule
reports it. Two rules in this repository were found dead that way, both
invisible from a clean report.

A release archive must be built with `git archive` rather than a desktop zip
tool: some tools write path separators that break installation on Linux.
`scripts/validate_zip.py` checks an archive for this.
