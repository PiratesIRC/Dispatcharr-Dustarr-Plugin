#!/usr/bin/env python3
"""Bump Metricsarr's calver version in plugin.py AND plugin.json in one shot.

Calver: 1.YY.DDDHHMM  (YY=2-digit year, DDD=day-of-year, HHMM=UTC time)
Hot-reload fires on plugin.json's mtime, so the two MUST stay in sync.
"""
import json
import pathlib
import re
import time

ROOT = pathlib.Path(__file__).resolve().parent
PLUGIN_PY = ROOT / "metricsarr" / "plugin.py"
PLUGIN_JSON = ROOT / "metricsarr" / "plugin.json"


def new_version():
    now = time.gmtime()
    return f"1.{now.tm_year % 100}.{now.tm_yday}{now.tm_hour:02d}{now.tm_min:02d}"


def main():
    version = new_version()

    src = PLUGIN_PY.read_text(encoding="utf-8")
    src, n = re.subn(r'^PLUGIN_VERSION = "[^"]+"',
                     f'PLUGIN_VERSION = "{version}"', src, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("PLUGIN_VERSION not found in plugin.py")
    PLUGIN_PY.write_text(src, encoding="utf-8", newline="\n")

    manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    manifest["version"] = version
    PLUGIN_JSON.write_text(json.dumps(manifest, indent=2) + "\n",
                           encoding="utf-8", newline="\n")

    print(f"bumped to {version}")


if __name__ == "__main__":
    main()
