"""Guard: the legacy name must not survive anywhere it could still bind.

This exists because the metricsarr -> dustarr rename touches identifiers that
are load-bearing at RUNTIME, not just prose: the Celery task import path is
derived from the package directory name, the Redis leader key is a literal, and
the Newsflasharr `source` string is matched by a routing rule that fails SILENTLY
(the event falls through to the default channels and the report email simply
stops arriving, with no error anywhere).

docs/ is deliberately excluded: dated specs and plans are a historical record and
docs/CHANGELOG.md must be free to say "renamed from Metricsarr".
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
LEGACY = re.compile(r"metricsarr", re.IGNORECASE)

# Everything whose contents can still bind something at runtime or in CI.
SCANNED_DIRS = ("dustarr", "tests", "scripts", ".github")
SCANNED_FILES = ("bump_version.py", "ruff.toml", "README.md", "CLAUDE.md",
                 "pytest.ini", "requirements-dev.txt")
SKIP_SUFFIXES = (".pyc",)
SKIP_PARTS = ("__pycache__", ".ruff_cache", ".pytest_cache")
# This guard names the thing it forbids; it cannot pass its own check.
SELF = "test_name_hygiene.py"


def _candidate_files():
    for rel in SCANNED_DIRS:
        for path in sorted((REPO / rel).rglob("*")):
            if not path.is_file():
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if path.name == SELF:
                continue
            yield path
    for name in SCANNED_FILES:
        path = REPO / name
        if path.is_file():
            yield path


def test_no_legacy_name_in_shipped_tree():
    offenders = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LEGACY.search(line):
                rel = path.relative_to(REPO).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()[:110]}")
    offender_list = "\n".join(offenders[:40])
    assert not offenders, (
        f"the legacy name survives in {len(offenders)} place(s):\n{offender_list}"
    )


def test_guard_actually_scans_something():
    """A guard that silently scans zero files passes forever and proves nothing.

    tests/test_report_sections.py exists in every checkout, so an empty sweep
    means the walker itself broke (a renamed directory, a bad skip rule), not
    that the tree is clean.

    After the rename the package MUST contribute, or the offenders test passes
    vacuously on an absent dustarr/. dustarr/plugin.json is the sentinel: if it
    is not present in the scanned set, the rename tasks have not completed.
    """
    scanned = {p.relative_to(REPO).as_posix() for p in _candidate_files()}
    assert "tests/test_report_sections.py" in scanned
    assert len(scanned) > 20, f"only {len(scanned)} files scanned"
    assert "dustarr/plugin.json" in scanned
