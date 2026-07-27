#!/usr/bin/env python3
"""Release-zip guard (bug-087).

PowerShell's Compress-Archive writes BACKSLASH path separators, which break
install on Dispatcharr's Linux host. zipfile.namelist() HIDES this, so parse the
raw central-directory bytes instead. Also rejects CRLF in .py files (bug-118).

Usage: python scripts/validate_zip.py Dustarr.zip
"""
import sys
import zipfile


def main(path):
    errors = []
    with open(path, "rb") as fh:
        raw = fh.read()
    if b"\\" in raw[:4] or raw.count(b"PK\x01\x02") == 0:
        errors.append("not a valid zip central directory")

    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            # The raw header name is the ground truth; namelist() normalizes.
            if "\\" in info.orig_filename:
                errors.append(f"backslash path separator: {info.orig_filename!r}")
            if info.orig_filename.endswith(".py"):
                if b"\r\n" in zf.read(info.filename):
                    errors.append(f"CRLF line endings: {info.orig_filename}")
        names = zf.namelist()

    if not any(n.endswith("plugin.py") for n in names):
        errors.append("missing plugin.py")
    if not any(n.endswith("__init__.py") for n in names):
        errors.append("missing package __init__.py")

    if errors:
        for err in errors:
            print(f"FAIL: {err}")
        sys.exit(1)
    print(f"OK: {path} ({len(names)} entries)")


if __name__ == "__main__":
    main(sys.argv[1])
