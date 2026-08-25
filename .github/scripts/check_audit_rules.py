"""Fail the build if the publish-audit rules are missing, malformed or undocumented.

The deny list is the half that catches real leaks here, because a provider brand
host, an M3U account suffix and a LAN prefix all look like ordinary words to a
generic scanner. Missing rules must therefore be an error, not a silent pass.

The rules themselves are NOT committed: they spell out the strings they exist to
protect, and a single-character class hides that from the scanner but not from a
person reading the file. See audit_rules.py for where they come from instead.

Kept as a FILE rather than inline in the workflow so the identical code can be
run locally before pushing. A workflow step that only ever executes on a runner
is a step nobody has tested.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from audit_rules import RulesUnavailable, load_rules  # noqa: E402

try:
    rules, source = load_rules()
except RulesUnavailable as exc:
    sys.exit(str(exc))

problems = []
for group, reason_key in (("deny", "why"), ("allow", "reason")):
    entries = rules.get(group)
    if not entries:
        problems.append(f"the {group} list is missing or empty")
        continue
    # Rules are named by POSITION, never by their text. A deny pattern spells
    # out the string it exists to keep out, and this output goes to a build log
    # that is public on a public repository. "deny rule 2" is enough to find the
    # offending entry in a local copy of the rules.
    for number, rule in enumerate(entries, start=1):
        pattern = rule.get("pattern")
        if not pattern:
            problems.append(f"{group} rule {number} has no pattern")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            # str(re.error) is the message and the position only; the
            # offending pattern is on exc.pattern, which is deliberately not
            # interpolated here. Measured rather than assumed.
            problems.append(f"{group} rule {number} does not compile: {exc}")
        if not rule.get(reason_key):
            problems.append(f"{group} rule {number} has no stated {reason_key}")

if problems:
    print("\n".join(problems))
    sys.exit(f"{len(problems)} problem(s) in the rules from {source}")

print(f"{len(rules['deny'])} deny and {len(rules['allow'])} allow rules "
      f"from {source}: all compile and all documented")
