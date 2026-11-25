"""Dustarr's storage paths: where its private state lives and what moves over from /data.

The logic (resolving the data directory, carrying the old /data state over and binding the state
constants) is in plugin_storage.py, a vendored copy of _shared/plugin_storage.py. This module holds
only what belongs to this plugin: the Spec, the table of state paths and the rollout switch. It is
named data_paths, not storage, because storage.py already holds Dustarr's usage.json reader and
writer. Each public function passes SPEC to the vendored module.
"""
from __future__ import annotations

try:
    from . import plugin_storage as _ps
except ImportError:  # loaded as a bare module, as plugin.py allows
    import plugin_storage as _ps

# Rollout switch for this release only: remove in the release after next, with the dual-path
# branches it guards. While it is on, a worker on this code also adopts the old /data files that
# workers on the previous release still write, when those are newer (see legacy_for and
# storage.adopt_newer_file).
ROLLOUT_DUAL_PATHS = True

# (constant name, kind, rel under the data dir). The usage file, the run stamp, the report counter
# and the notify gate state move into the data directory. The report fallback folder is a
# nocopy row named by reports_constant: it moves to the shared Newsflasharr reports folder and
# the old reports are not carried over. The usage reporter's folder moves under the data dir.
_BIND_TABLE = (
    ("USAGE_FILE_PATH", "file", "usage.json"),
    ("SCHEDULED_RUN_PATH", "file", "scheduled_run.json"),
    ("REPORT_COUNT_PATH", "file", "report_count.json"),
    ("NOTIFY_STATE_PATH", "file", "notify_state.json"),
    ("USAGE_DATA_DIR", "usage_dir", "usage"),
    ("REPORT_FALLBACK_DIR", "nocopy", None),
)

SPEC = _ps.Spec(
    label="Dustarr",
    state_name="_dustarr_storage_state",
    table=_BIND_TABLE,
    beside={},
    reports_subdir="dustarr",
    usage_subdir="dustarr",
    reports_constant="REPORT_FALLBACK_DIR",
)

Layout = _ps.Layout
Item = _ps.Item
MigrateResult = _ps.MigrateResult
atomic_write_bytes = _ps.atomic_write_bytes
atomic_write_json = _ps.atomic_write_json


def resolve(plugin_dir, key, logger):
    """Resolve the data directory for the plugin folder plugin_dir. See plugin_storage.resolve."""
    return _ps.resolve(SPEC, plugin_dir, key, logger)


def migrate(layout, items, logger, version=None):
    """Copy the old state into the data dir once. See plugin_storage.migrate."""
    return _ps.migrate(SPEC, layout, items, logger, version=version)


def items_for(cfg, layout):
    """The moves bind hands to migrate. See plugin_storage.items_for."""
    return _ps.items_for(SPEC, cfg, layout)


def bind(cfg, plugin_file, logger):
    """Resolve, rebind the state constants and carry the old state over.

    On top of plugin_storage.bind: when the process is bound to a data directory, DATA_DIR is
    rebound to it too. DATA_DIR is not a table row, because a row copies state and the data
    directory itself holds no state of its own. A legacy or failed bind leaves DATA_DIR as written.
    """
    layout = _ps.bind(SPEC, cfg, plugin_file, logger)
    if not layout.legacy:
        cfg.DATA_DIR = layout.data_dir
    return layout


def legacy_for(cfg, name):
    """The old /data path of constant name while the rollout flag is on and cfg is bound, else None.

    Only a bound process has a second copy of the state to adopt from. A legacy process reads the
    old paths itself, so there is nothing to adopt.
    """
    if not ROLLOUT_DUAL_PATHS:
        return None
    layout = getattr(cfg, "STORAGE_LAYOUT", None)
    if layout is None or layout.legacy:
        return None
    return (getattr(cfg, "LEGACY_PATHS", None) or {}).get(name)


def check_context(layout, context, logger):
    """Warn once when Dispatcharr's data_dir differs from the resolved one. See plugin_storage."""
    return _ps.check_context(SPEC, layout, context, logger)
