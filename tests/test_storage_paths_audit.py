"""Every /data path literal in the shipped modules is one the storage design accounts for.

plugin.py holds its /data literals only as the module-level constants that data_paths.bind moves:
the legacy state paths, the usage reporter's folder and the report fallback folder. The vendored
clients keep their own defaults, which the plugin overrides by passing data_dir (usage_client)
or never uses (notify_client). A literal anywhere else is a path the data-dir move does not cover.
"""
import ast
import pathlib

PKG_DIR = pathlib.Path(__file__).resolve().parent.parent / "dustarr"

# The vendored clients are not edited; the plugin never uses their defaults for its own state.
VENDORED_ALLOWED = {
    "usage_client.py": {"/data/plugin_stats"},
    "notify_client.py": {"/data/newsflasharr"},
}
# The storage modules name legacy paths in docstrings; plugin_storage.py guards real data.
SKIPPED = {"data_paths.py", "plugin_storage.py"}

PLUGIN_LITERALS = {
    "/data/dustarr", "/data/dustarr/usage.json", "/data/dustarr/scheduled_run.json",
    # Read by Newsflasharr (notifier/newsflasharr/report_count.py) at /data/<source>/report_count.json,
    # so it is allowed to stay at this path in every mode and is never moved by bind.
    "/data/dustarr/report_count.json",
    "/data/dustarr/notify_state.json",
    "/data/dustarr/reports", "/data/plugin_stats",
}


def _data_literals(tree):
    """(value, node) for each string constant starting with /data and holding no whitespace."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith("/data") and not any(ch.isspace() for ch in value):
                yield value, node


def test_plugin_py_keeps_data_literals_only_in_the_constant_block():
    path = PKG_DIR / "plugin.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constant_names = {"DATA_DIR", "USAGE_FILE_PATH", "SCHEDULED_RUN_PATH", "REPORT_COUNT_PATH",
                      "NOTIFY_STATE_PATH", "USAGE_DATA_DIR", "REPORT_FALLBACK_DIR"}
    allowed_nodes = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in constant_names
                                                for t in node.targets):
            allowed_nodes.update(id(n) for n in ast.walk(node.value))
    found = list(_data_literals(tree))
    outside = sorted({v for v, n in found if id(n) not in allowed_nodes})
    assert not outside, "a /data literal outside the bound constants: %s" % outside
    assert {v for v, _n in found} == PLUGIN_LITERALS


def test_no_other_module_holds_a_data_literal_outside_the_allowed_set():
    for path in sorted(PKG_DIR.glob("*.py")):
        if path.name in SKIPPED or path.name == "plugin.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {v for v, _n in _data_literals(tree)}
        strays = found - VENDORED_ALLOWED.get(path.name, set())
        assert not strays, "%s holds unaccounted /data literals: %s" % (path.name, sorted(strays))
