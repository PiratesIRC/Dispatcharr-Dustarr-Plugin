"""Fail the build if .publish-audit.json is missing, malformed, or undocumented.

The deny list is the half that catches real leaks here, because a provider brand
host, an M3U account suffix and a LAN prefix all look like ordinary words to a
generic scanner. A missing rules file must therefore be an error, not a silent
pass.

Kept as a FILE rather than inline in the workflow so the identical code can be
run locally before pushing. A workflow step that only ever executes on a runner
is a step nobody has tested.
"""
import json
import pathlib
import re
import sys

RULES_FILE = pathlib.Path(".publish-audit.json")

if not RULES_FILE.exists():
    sys.exit(".publish-audit.json is missing: the deny list is the half that "
             "catches real leaks in this repository")

rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))

problems = []
for group, reason_key in (("deny", "why"), ("allow", "reason")):
    entries = rules.get(group)
    if not entries:
        problems.append(f"the {group} list is missing or empty")
        continue
    for rule in entries:
        pattern = rule.get("pattern")
        if not pattern:
            problems.append(f"a {group} rule has no pattern")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"{group} rule {pattern!r} does not compile: {exc}")
        if not rule.get(reason_key):
            problems.append(f"{group} rule {pattern!r} has no stated {reason_key}")

if problems:
    print("\n".join(problems))
    sys.exit(f"{len(problems)} problem(s) in .publish-audit.json")

print(f"{len(rules['deny'])} deny and {len(rules['allow'])} allow rules, "
      f"all compile and all documented")
