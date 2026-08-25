"""Scan every tracked file against the repository-specific deny list.

Second line of defence behind the workspace audit script, which is not committed
here. This applies ONLY the deny and allow lists: it does not run the built-in
credential, hostname and entropy patterns, and it cannot scan history. Run the
full workspace audit before pushing regardless.

Kept as a FILE rather than inline in the workflow so the identical code can be
run locally before pushing.

`git ls-files` is the authority on what would be published, not .gitignore: a
file committed before an ignore rule was added is still tracked.
"""
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from audit_rules import RulesUnavailable, load_rules  # noqa: E402

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".woff", ".woff2"}

try:
    rules, source = load_rules()
except RulesUnavailable as exc:
    sys.exit(str(exc))
# Rules are carried by POSITION, not by their text. Continuous integration logs
# follow repository visibility, so anything printed here is published on a
# public repository, and a deny pattern names the exact string it exists to
# keep out. Reporting "deny rule 2" keeps a finding actionable without
# republishing the pattern: a maintainer counts to the second entry in their
# own copy of the rules.
deny = [(re.compile(r["pattern"], re.I), number)
        for number, r in enumerate(rules["deny"], start=1)]
allow = [re.compile(r["pattern"], re.I) for r in rules["allow"]]

listed = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
files = [f for f in listed.stdout.splitlines() if f]

findings = []
skipped = []
for name in files:
    path = pathlib.Path(name)
    if path.suffix.lower() in SKIP_SUFFIXES:
        skipped.append(name)
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        skipped.append(name)
        continue
    for regex, number in deny:
        for match in regex.finditer(text):
            if any(a.search(match.group(0)) for a in allow):
                continue
            line = text.count("\n", 0, match.start()) + 1
            # The matched TEXT is not printed either: on a real finding it IS
            # the secret.
            findings.append(f"{name}:{line} matched deny rule {number}")

print(f"scanned {len(files) - len(skipped)} tracked file(s) against the rules "
      f"from {source}, skipped {len(skipped)} binary or undecodable")
for name in skipped:
    print("  not scanned:", name)

if findings:
    print()
    print("\n".join(sorted(set(findings))))
    sys.exit(f"{len(set(findings))} finding(s): read every one before this "
             f"reaches the remote. Rules are numbered by their position in "
             f"the deny list; the patterns are deliberately not printed, "
             f"because this output is published on a public repository.")

print("no deny-list findings")
