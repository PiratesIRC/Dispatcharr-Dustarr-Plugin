# Contributing

Thank you for considering a contribution. Bug reports, feature requests and pull
requests are all welcome.

## Before you open an issue

Please do not include provider credentials, stream URLs, M3U account names or
server addresses. A stream URL usually carries a username and password in its
path, and an M3U account name is often the provider's hostname. Both identify
your account to anyone reading the issue.

Channel names are usually safe to share and are often necessary to explain a
problem. Your full channel list is a different matter: it identifies your
subscription, so quote the few channels that matter rather than attaching the
CSV export.

Useful things to include instead:

- The plugin version from the plugin card.
- What **Validate settings** reported. It writes nothing and it names the
  problem in most cases: it checks every setting parses, whether the collector
  is running, whether the schedule exists and is queued to a worker that will
  actually run it, and whether email can go out.
- What **Show summary** reported, which is the tracking window, the coverage and
  the never-watched count.

For a security vulnerability, use private reporting instead. See
[SECURITY.md](SECURITY.md).

## Setting up

The plugin runs inside Dispatcharr's Django backend, so there is no standalone
way to run it. Tests are the safety net, and they stub Django so the plugin
imports in isolation.

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

The suite takes a few seconds. Both must pass before a pull request is reviewed.

Optionally, enable the pre-commit hook once per clone. It byte-compiles the
plugin, checks the version files agree, scans the tree against the credential
deny list and runs the tests:

```bash
git config core.hooksPath .githooks
```

## Making a change

**Write the test first.** Watch it fail, then make it pass. A test written after
the code passes immediately, which proves it runs but not that it can catch the
bug. Most of the tests in `tests/` exist because something broke once, and each
records what and why.

**Deliberately break a new guard before trusting it.** A test that has never
failed may not be testing anything. If you cannot make it fail by breaking the
code it covers, it is not yet a test. One guard in this repository checked
nothing for weeks because a line-anchored regular expression matched only the
first declaration on each line.

**Judge a test run by its exit code, not by reading the output.** A pipeline
that greps the output can report success while the run failed.

**Keep the change scoped.** One change per pull request, without unrelated
reformatting mixed in, so a reviewer can see what actually changed.

**Do not reset module-level state by hand in a test.** The `plugin` fixture
clears the collector restart budget for you. State that leaks between tests
produces a failure in an unrelated test, which sends the next person to the
wrong place.

## The constraints that are not obvious

- **The plugin is read-only and a test enforces it.** See
  [SECURITY.md](SECURITY.md). If you add a feature that needs to write to
  Dispatcharr, read the docstring at the top of `tests/test_no_mutations.py`
  first: the guard fails when it cannot prove what a call is made on, and that
  default is deliberate.
- **`collector.py`, `sessionizer.py`, `storage.py`, `gates.py` and
  `redaction.py` use the standard library only.** The collector runs in a
  background thread that must never touch the database. Tests enforce this.
- **`gateway.py` and `reports.py` keep their Django imports inside functions.**
  A Django import at module level breaks Dispatcharr's plugin loader. Tests
  enforce this too.
- **Every helper a renderer calls must handle every input.** The report writer
  catches file errors only, so a division by zero or a wrong type escapes and
  the report is not written at all. Not-a-number, both infinities, `None`, an
  unparseable string and a zero denominator all have to produce something.
- **No em dashes, en dashes, double hyphens or contractions in anything the user
  reads**, which includes the report, the settings help text and the info
  panels. A double hyphen renders as an em dash on the page. A test checks the
  whole rendered report.
- **No contractions in code, comments, docstrings or test names.** Write "does
  not", not "doesn't". Possessives are fine.
- Comments should say why, not what. The code already says what.

## Versioning

This plugin uses calendar versioning, `Major.YY.DDDHHMM`. Bump it with the
script, which keeps `dustarr/plugin.json` and `dustarr/plugin.py` in step:

```bash
python scripts/bump_version.py
```

Do not edit either version by hand. A mismatch fails the build.

## What runs on your pull request

- A check that `dustarr/plugin.json` and `dustarr/plugin.py` agree on the version
- `ruff check .`
- The full test suite
- A publish audit, which fails if the tree contains a provider hostname, an M3U
  account suffix, a local network address or a personal path

## Questions

Open an issue on the
[issue tracker](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/issues).
