"""The audit scripts must never print a deny pattern or its reason.

Continuous integration logs follow repository visibility. On a public repository
they are public, so anything these scripts print is published. The deny rules
name the exact strings they exist to keep out of the tree, which is why the
rules file itself is not committed, and printing a pattern into a build log
would republish it by another route.

The scripts must still be USEFUL when they find something: the file and line
have to be reported, or nobody can act on the finding. What they report instead
of the pattern is its position in the deny list, which a maintainer resolves
against their own local copy of the rules.

These tests drive the real scripts as subprocesses in a throwaway git
repository, rather than importing them, because both read `git ls-files` and
both are meant to be run exactly the way the workflow runs them.
"""
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".github" / "scripts"

# A pattern whose plain form appears nowhere else in this repository, and whose
# single-character class is written the same way the real rules are.
SECRET_WORD = "zzq[u]uxsecret"
SECRET_PLAIN = "zzquuxsecret"
DENY_REASON = "a reason that must not reach a build log either"

RULES = {
    "deny": [
        {"pattern": "neverm[a]tchesanything", "why": "first rule, deliberately inert"},
        {"pattern": SECRET_WORD, "why": DENY_REASON},
    ],
    "allow": [
        {"pattern": "unrel[a]tedallow", "reason": "present so the allow list is not empty"},
    ],
}


def _run(script, cwd, rules=RULES):
    env = {"PUBLISH_AUDIT_RULES": json.dumps(rules), "PATH": _path(), "SYSTEMROOT": _systemroot()}
    result = subprocess.run([sys.executable, str(SCRIPTS / script)], cwd=cwd,
                            capture_output=True, text=True, env=env)
    return result


def _path():
    import os
    return os.environ.get("PATH", "")


def _systemroot():
    import os
    return os.environ.get("SYSTEMROOT", "")


@pytest.fixture
def repo_with_a_planted_match(tmp_path):
    """A git repository holding one tracked file that trips the second rule."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    offender = tmp_path / "offender.txt"
    offender.write_text(f"harmless line\nthis line carries {SECRET_PLAIN} in it\n",
                        encoding="utf-8")
    subprocess.run(["git", "add", "offender.txt"], cwd=tmp_path, check=True)
    return tmp_path


def test_scan_reports_the_finding_at_all(repo_with_a_planted_match):
    """Guard against the opposite failure: hiding the pattern by finding nothing.

    If this scan stopped detecting the planted string, the test below would pass
    for entirely the wrong reason, because a pattern that is never matched is
    also a pattern that is never printed.
    """
    result = _run("scan_tree.py", repo_with_a_planted_match)
    assert result.returncode != 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "offender.txt:2" in output, output


def test_scan_does_not_print_the_pattern_or_its_reason(repo_with_a_planted_match):
    result = _run("scan_tree.py", repo_with_a_planted_match)
    output = result.stdout + result.stderr
    assert SECRET_WORD not in output, "the deny pattern reached the output"
    assert DENY_REASON not in output, "the deny rule's reason reached the output"


def test_scan_identifies_the_rule_by_its_position(repo_with_a_planted_match):
    """The finding has to stay actionable without naming the pattern."""
    result = _run("scan_tree.py", repo_with_a_planted_match)
    output = result.stdout + result.stderr
    # The planted match is the SECOND deny rule, so it is rule 2, not rule 1.
    assert "rule 2" in output, output
    assert "rule 1" not in output, output


def test_rule_check_does_not_print_a_pattern_that_fails_to_compile(tmp_path):
    """The validator prints problems, and a problem used to quote the pattern."""
    broken = {
        "deny": [{"pattern": SECRET_WORD + "(", "why": DENY_REASON}],
        "allow": [{"pattern": "fi[n]e", "reason": "present so the allow list is not empty"}],
    }
    result = _run("check_audit_rules.py", tmp_path, rules=broken)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert SECRET_WORD not in output, "the deny pattern reached the output"
    assert "does not compile" in output, output
    # The reason must survive, or the report is useless for fixing the rule.
    # str(re.error) carries the message and position but not the pattern, which
    # lives on exc.pattern; that was measured, not assumed.
    assert "unterminated subpattern" in output, output


def test_rule_check_does_not_print_a_pattern_that_lacks_a_reason(tmp_path):
    undocumented = {
        "deny": [{"pattern": SECRET_WORD, "why": ""}],
        "allow": [{"pattern": "fi[n]e", "reason": "present so the allow list is not empty"}],
    }
    result = _run("check_audit_rules.py", tmp_path, rules=undocumented)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert SECRET_WORD not in output, "the deny pattern reached the output"
    assert "no stated why" in output, output
