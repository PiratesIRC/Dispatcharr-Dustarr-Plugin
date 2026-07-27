"""Vendored notify_client.py drift gate -- matching_core parity pattern.

The committed vendored copy (dustarr/notify_client.py) must hash-match both
scripts/client_manifest.json (Layer A: catches a hand-edit to the vendored
copy) and the workspace-level _shared/notify_client.py canonical source
(Layer B: catches the manifest itself drifting from the source of truth).
To land an intended client change: edit ../_shared/notify_client.py, copy it
byte-identically over dustarr/notify_client.py, recompute the sha256, and
update client_manifest.json.

NOTE: _shared/ lives outside this git repo (workspace-level, sibling to
dustarr/), so Layer B only runs where that directory is present (e.g. the
full workspace checkout). A repo-only checkout (e.g. isolated CI) will not
have it -- see notifier's task-2 report for this environment-fragility
tradeoff.
"""
import hashlib
import json
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDORED = os.path.join(_REPO, "dustarr", "notify_client.py")
_MANIFEST = os.path.join(_REPO, "scripts", "client_manifest.json")
_SHARED = os.path.join(os.path.dirname(_REPO), "_shared", "notify_client.py")

with open(_MANIFEST, encoding="utf-8") as _fh:
    _PINS = json.load(_fh)


def _sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_vendored_client_matches_manifest():
    assert os.path.exists(_VENDORED), "vendored notify_client.py is missing"
    digest = _sha256(_VENDORED)
    assert digest == _PINS["notify_client.py"], (
        "notify_client.py vendored copy drifted from its pinned hash. If "
        "intended, edit _shared/notify_client.py, re-vendor, and update "
        "client_manifest.json."
    )


@pytest.mark.skipif(not os.path.exists(_SHARED),
                     reason="workspace _shared/notify_client.py not present "
                            "in this checkout")
def test_vendored_client_matches_shared_source():
    assert _sha256(_VENDORED) == _sha256(_SHARED), (
        "dustarr/notify_client.py is not byte-identical to "
        "../_shared/notify_client.py (the canonical source of truth)."
    )
