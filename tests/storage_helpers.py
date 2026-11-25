"""Helpers shared by the storage tests. Nothing here reads a real /data path."""
import ast
import os
import pathlib
from types import SimpleNamespace

PLUGIN_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "dustarr" / "plugin.py"
# Every constant the carry-over moves, plus the usage reporter's folder and the reports folder.
STATE_NAMES = ("USAGE_FILE_PATH", "SCHEDULED_RUN_PATH", "NOTIFY_STATE_PATH",
               "USAGE_DATA_DIR", "REPORT_FALLBACK_DIR")


class Log:
    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, *a):
        self.infos.append(a)

    def debug(self, *a):
        pass

    def warning(self, *a):
        self.warnings.append(a)

    def error(self, *a):
        self.errors.append(a)


def source_literals():
    """The module-level string constants of plugin.py, read from source (the legacy paths)."""
    tree = ast.parse(PLUGIN_SOURCE.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    return out


def make_cfg(values):
    """A stand-in for the plugin module: bind reads and writes plain attributes on it.
    Only a dict of legacy paths is passed through as LEGACY_PATHS, which items_for reads."""
    ns = {"PLUGIN_DB_KEY": "dustarr", "PLUGIN_VERSION": "9.9.9", "LEGACY_PATHS": dict(values)}
    ns.update(values)
    return SimpleNamespace(**ns)


def make_layout(data_paths, root):
    plugins = os.path.join(root, "plugins")
    data = os.path.join(plugins, "_data", "dustarr")
    return data_paths.Layout(False, data, plugins, "dustarr", "test")


def tmp_legacy(tmp_path):
    """The legacy literals, each moved under tmp_path so a carry-over reads only test files."""
    base = tmp_path / "old"
    literals = source_literals()
    return {name: str(base / literals[name][len("/data/"):]) for name in STATE_NAMES}
