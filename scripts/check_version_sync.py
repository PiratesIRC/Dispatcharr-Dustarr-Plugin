#!/usr/bin/env python3
"""CI gate: plugin.py's PLUGIN_VERSION must equal plugin.json's version."""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
py = (ROOT / "dustarr" / "plugin.py").read_text(encoding="utf-8")
match = re.search(r'^PLUGIN_VERSION = "([^"]+)"', py, flags=re.M)
if not match:
    sys.exit("PLUGIN_VERSION not found in plugin.py")
manifest = json.loads((ROOT / "dustarr" / "plugin.json").read_text(encoding="utf-8"))
if match.group(1) != manifest["version"]:
    sys.exit(f"version mismatch: plugin.py={match.group(1)} "
             f"plugin.json={manifest['version']}")
print(f"version in sync: {match.group(1)}")
