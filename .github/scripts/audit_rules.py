"""Load the repository-specific publish-audit rules.

The rules name the exact strings that must never be published: a provider brand
host, M3U account name fragments, a local network prefix, a personal drive path.
Each pattern is written with a single-character class so the rules do not match
themselves while scanning.

That trick defeats the SCANNER. It does not defeat a PERSON reading the file, so
the rules must not live in the published tree of a repository that may become
public. They are supplied one of two ways:

1. `PUBLISH_AUDIT_RULES`, holding the rules as JSON. This is how continuous
   integration gets them, from a repository secret.
2. `.publish-audit.json` in the repository root, which is gitignored and exists
   only on a maintainer's own machine.

If neither is present this raises. That direction is deliberate and matches the
rest of this repository: a check whose input is missing must fail loudly, never
report a clean result it could not actually compute. A silent pass here would be
indistinguishable from a genuinely clean scan.
"""
import json
import os
import pathlib

ENV_VAR = "PUBLISH_AUDIT_RULES"
RULES_FILE = pathlib.Path(".publish-audit.json")


class RulesUnavailable(Exception):
    """Neither source supplied the rules. Never treat this as a clean scan."""


def load_rules():
    """Return (rules dict, a short description of where they came from)."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        try:
            return json.loads(raw), f"the {ENV_VAR} environment variable"
        except json.JSONDecodeError as exc:
            raise RulesUnavailable(
                f"{ENV_VAR} is set but does not parse as JSON: {exc}. "
                f"Fix the secret rather than unsetting it: an unset variable "
                f"would fall back to a file that does not exist on a runner, "
                f"and the scan would stop covering anything.") from exc

    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text(encoding="utf-8")), str(RULES_FILE)

    raise RulesUnavailable(
        f"no publish-audit rules available: {ENV_VAR} is unset or empty and "
        f"{RULES_FILE} does not exist. The deny list is the half that catches "
        f"real leaks in this repository, so this is an error rather than a "
        f"reason to skip the scan. On a runner, set the repository secret that "
        f"populates {ENV_VAR}. On a maintainer machine, restore {RULES_FILE} "
        f"(it is gitignored, so it is never restored by a clone).")
