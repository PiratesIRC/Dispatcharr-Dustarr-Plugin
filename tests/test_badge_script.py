"""The README must show the plugin-stats Worker badges, never the retired gist.

The Reports Built badge was published through a Gist until 2026-10-10. The gist
id is kept here as a literal, because the id file it used to be read from has
been removed, so the public count cannot silently revert to the gist.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
RETIRED_GIST_ID = "b7116c2e44b8250a4995dfe1836da70b"


def test_the_readme_shows_the_worker_badges_not_the_gist():
    """The README badges read the plugin-stats Worker. The retired gist id must
    not come back, or the public count silently reverts to the gist."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "https://plugin-stats.dpas.workers.dev/badge/dustarr/reports_built" in readme,         "README.md does not carry the Reports Built Worker badge"
    assert "https://plugin-stats.dpas.workers.dev/badge/dustarr/installs" in readme,         "README.md does not carry the Active Installs Worker badge"
    assert RETIRED_GIST_ID not in readme, "README.md still points at the retired gist"
