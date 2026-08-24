"""Render a representative report to tests/fixtures/sample_report.html.

The fixture is the artifact behind the visual-verification claim: a later
session can diff it instead of trusting "I looked at it". Re-run and commit
whenever the renderer changes:

    python scripts/render_sample.py

Run from the repo root (not from inside scripts/) so the sys.path insert
below resolves tests/conftest.py correctly.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from conftest import load_plugin, sample_model  # noqa: E402

load_plugin()

rp = sys.modules["dustarr_under_test.reports"]
gw = sys.modules["dustarr_under_test.gateway"]

# The model is built in tests/conftest.py so this script and the test that
# compares against the fixture cannot drift apart.
html = rp.render_html(sample_model(rp, gw))
out = ROOT / "tests" / "fixtures" / "sample_report.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(html, encoding="utf-8", newline="\n")
print(f"wrote {out} ({len(html)} bytes)")
