#!/usr/bin/env python3
"""Refresh the public "Reports Built" badge on the README.

WHAT THIS DOES. Reads how many usage reports this plugin has successfully
written, from its own counter file inside the Dispatcharr container, and writes
a Shields.io endpoint document to a GitHub Gist. The README badge points at that
Gist, so this script is what makes the public number change.

    python scripts/update_reports_built_badge.py            # refresh the Gist
    python scripts/update_reports_built_badge.py --dry-run  # print, write nothing
    python scripts/update_reports_built_badge.py --create   # first-time Gist setup

WHAT COUNTS AS ONE. One report whose HTML file was confirmed on disk, whether
the Build report button or the weekly schedule produced it. The plugin bumps the
counter only after that check (plugin.bump_report_count), so a build that failed
to write is not counted. It is not a delivery count: with notifications off it
still increments. The full contract is docs/report-count-integration.md.

WHERE THE NUMBER COMES FROM. /data/dustarr/report_count.json inside the
container, one key, one non-negative integer, rewritten atomically by the
plugin. Nothing here is reconstructed: the file has existed since the counter
shipped and matched the report files on disk when this badge went live.

DEGRADATION. Anything the file does not say plainly reads as 0, the same
direction as plugin.read_report_count: a badge must never invent activity. A
Gist that suddenly reads 0 is therefore a signal that the file or the container
is unreadable from this machine, not that the reports stopped.

PRIVACY. The counter file holds one integer and nothing else. Only that integer
reaches the Gist. The Gist is unlisted rather than private and the README names
it, so treat the number as public.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CONTAINER = "dispatcharr"
COUNT_FILE = "/data/dustarr/report_count.json"
GIST_FILENAME = "dustarr-reports-built.json"
GIST_DESCRIPTION = "Dustarr reports-built badge (Shields.io endpoint)"
BADGE_LABEL = "Reports Built"
BADGE_COLOUR = "blueviolet"

# gh is installed and authenticated but is NOT on PATH in either shell here, so
# `command -v gh` reports it missing and is not evidence. The path is built from
# LOCALAPPDATA rather than written out in full: this repository is public and a
# literal path names the Windows account for no benefit.
GH = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                  "Packages",
                  "GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe",
                  "bin", "gh.exe")

# Where the Gist id is remembered between runs. It is committed, so a re-clone
# keeps updating the same document rather than silently creating a second one.
# The id is not a secret: the README badge URL names it.
GIST_ID_FILE = ROOT / "scripts" / ".reports_built_badge_gist"


def parse_count(text):
    """-> int, the published-report total, or 0 for anything untrustworthy.

    Mirrors plugin.read_report_count: missing, empty, corrupt, wrong shape,
    negative or non-numeric all read as 0. A public number that cannot be
    trusted must go to nothing rather than to a guess.
    """
    if not text:
        return 0
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return 0
        value = data.get("reports_built", 0)
        if isinstance(value, bool) or value is None:
            return 0
        value = int(value)
    except (ValueError, TypeError):
        return 0
    return value if value >= 0 else 0


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check)


def read_count():
    # MSYS_NO_PATHCONV is irrelevant here: subprocess passes the container
    # path straight to docker.exe with no shell in between.
    result = _docker("exec", CONTAINER, "sh", "-c",
                     f"cat {COUNT_FILE} 2>/dev/null || true", check=False)
    return parse_count(result.stdout)


def read_gist_id():
    if GIST_ID_FILE.exists():
        value = GIST_ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def endpoint_document(total):
    return {
        "schemaVersion": 1,
        "label": BADGE_LABEL,
        "message": f"{total:,}",
        "color": BADGE_COLOUR,
    }


def publish(total, create=False, dry_run=False):
    document = json.dumps(endpoint_document(total), indent=2)
    if dry_run:
        print(document)
        return 0

    if not os.path.exists(GH):
        print(f"the GitHub CLI was not found at {GH}", file=sys.stderr)
        return 1

    tmp = ROOT / "scripts" / GIST_FILENAME
    tmp.write_text(document, encoding="utf-8")
    try:
        if create:
            result = subprocess.run(
                [GH, "gist", "create", str(tmp), "--desc", GIST_DESCRIPTION],
                capture_output=True, text=True)
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                return result.returncode
            url = result.stdout.strip().splitlines()[-1]
            gist_id = url.rstrip("/").split("/")[-1]
            GIST_ID_FILE.write_text(gist_id + chr(10), encoding="utf-8")
            print(f"created gist {gist_id}")
            print("badge URL:")
            print(f"  https://img.shields.io/endpoint?url=https://gist."
                  f"githubusercontent.com/PiratesIRC/{gist_id}/raw/{GIST_FILENAME}")
            return 0

        gist_id = read_gist_id()
        if not gist_id:
            print("no gist id recorded; run once with --create", file=sys.stderr)
            return 1
        result = subprocess.run(
            [GH, "gist", "edit", gist_id, "-f", GIST_FILENAME, str(tmp)],
            capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return result.returncode
        print(f"gist {gist_id} updated to {total:,}")
        return 0
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be published, write nothing")
    parser.add_argument("--create", action="store_true",
                        help="create the Gist for the first time")
    args = parser.parse_args(argv)

    total = read_count()
    print(f"total reports built: {total:,}")
    return publish(total, create=args.create, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
