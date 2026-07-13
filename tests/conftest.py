"""Shared fixtures for Metricsarr tests.

sessionizer.py / storage.py / collector.py / gates.py are stdlib-only and are
loaded directly from file paths (no stubs). plugin.py / gateway.py / reports.py
import Django lazily inside functions; the stubs below cover what tests exercise.
"""
import fnmatch
import functools
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "metricsarr"
_PKG = "metricsarr_under_test"


def _install_runtime_stubs():
    apps = types.ModuleType("apps")
    apps.__path__ = []
    apps_channels = types.ModuleType("apps.channels")
    apps_channels.__path__ = []
    models = types.ModuleType("apps.channels.models")
    for name in ("Channel", "ChannelGroup", "ChannelProfile",
                 "ChannelProfileMembership"):
        setattr(models, name, MagicMock(name=name))
    django = types.ModuleType("django")
    django_db = types.ModuleType("django.db")
    django_db.close_old_connections = MagicMock(name="close_old_connections")
    core = types.ModuleType("core")
    core.__path__ = []
    core_utils = types.ModuleType("core.utils")
    core_utils.RedisClient = MagicMock(name="RedisClient")
    core_sched = types.ModuleType("core.scheduling")
    core_sched.create_or_update_periodic_task = MagicMock(name="create_task")
    core_sched.delete_periodic_task = MagicMock(name="delete_task")
    sys.modules.update({
        "apps": apps, "apps.channels": apps_channels,
        "apps.channels.models": models,
        "django": django, "django.db": django_db,
        "core": core, "core.utils": core_utils, "core.scheduling": core_sched,
    })

    # celery is imported at MODULE scope in plugin.py (the C1 fix -- the
    # @shared_task decorator is what registers the task, not the eager
    # plugin import), so it is only ever imported ONCE per test session
    # (plugin.py itself is cached in sys.modules after the first load_plugin()
    # call). Recreating this module on every call, like the stubs above,
    # would silently orphan the registration recorded at that first import --
    # so install it once and reuse it.
    if "celery" not in sys.modules:
        celery_mod = types.ModuleType("celery")
        celery_mod.registered_tasks = {}

        def _fake_shared_task(*dargs, **dkwargs):
            """Records registration (celery_mod.registered_tasks) so C1 is
            actually testable. Supports both bare (@shared_task) and
            parameterized (@shared_task(name=...)) usage, like real Celery."""

            def _register(fn):
                name = dkwargs.get("name") or f"{fn.__module__}.{fn.__qualname__}"

                @functools.wraps(fn)
                def _task(*args, **kwargs):
                    return fn(*args, **kwargs)

                _task.name = name
                _task.delay = _task
                _task.run = fn
                celery_mod.registered_tasks[name] = _task
                return _task

            if dargs and callable(dargs[0]) and not dkwargs:
                return _register(dargs[0])
            return _register

        celery_mod.shared_task = _fake_shared_task
        sys.modules["celery"] = celery_mod


def _load_from_path(module_name, file_name):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_pure(name):
    """Load a stdlib-only module (sessionizer/storage/collector/gates/webhook)."""
    key = f"{_PKG}_{name}"
    if key not in sys.modules:
        _load_from_path(key, f"{name}.py")
    return sys.modules[key]


def load_plugin():
    """Load plugin.py as a package submodule so relative imports resolve."""
    _install_runtime_stubs()
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(PLUGIN_DIR)]
        sys.modules[_PKG] = pkg
    for mod in ("sessionizer", "storage", "collector", "gates", "gateway",
                "webhook", "reports", "plugin"):
        key = f"{_PKG}.{mod}"
        if key not in sys.modules and (PLUGIN_DIR / f"{mod}.py").exists():
            _load_from_path(key, f"{mod}.py")
    return sys.modules[f"{_PKG}.plugin"]


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def wall(self):
        return self.t

    def mono(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakePipeline:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def __getattr__(self, name):
        def op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return op

    def execute(self):
        return [getattr(self.r, n)(*a, **k) for n, a, k in self.ops]


class FakeRedis:
    """Dict-backed stub of the Redis commands Metricsarr uses."""

    def __init__(self, clock=None, bytes_mode=False):
        self.clock = clock or FakeClock()
        self.bytes_mode = bytes_mode
        self.kv = {}
        self.hashes = {}
        self.sets = {}
        self.exp = {}

    def _alive(self, key):
        exp = self.exp.get(key)
        if exp is not None and self.clock.wall() >= exp:
            for store in (self.kv, self.hashes, self.sets):
                store.pop(key, None)
            self.exp.pop(key, None)
            return False
        return key in self.kv or key in self.hashes or key in self.sets

    def _out(self, s):
        if s is None:
            return None
        return s.encode("utf-8") if self.bytes_mode else s

    def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return None
        self.kv[key] = str(value)
        if ex is not None:
            self.exp[key] = self.clock.wall() + ex
        else:
            self.exp.pop(key, None)
        return True

    def get(self, key):
        return self._out(self.kv.get(key)) if self._alive(key) else None

    def delete(self, key):
        existed = self._alive(key)
        for store in (self.kv, self.hashes, self.sets):
            store.pop(key, None)
        self.exp.pop(key, None)
        return 1 if existed else 0

    def exists(self, key):
        return 1 if self._alive(key) else 0

    def expire(self, key, ttl):
        if self._alive(key):
            self.exp[key] = self.clock.wall() + ttl
            return True
        return False

    def scard(self, key):
        return len(self.sets.get(key, set())) if self._alive(key) else 0

    def scan_iter(self, match=None, count=None):
        for key in list(self.kv) + list(self.hashes) + list(self.sets):
            if self._alive(key) and (match is None or fnmatch.fnmatch(key, match)):
                yield key.encode() if self.bytes_mode else key

    def pipeline(self):
        return FakePipeline(self)

    # --- test helpers -----------------------------------------------------
    def open_channel(self, uuid, clients=1, ttl=30.0):
        """Simulate a live channel: metadata hash + clients set."""
        self.hashes[f"live:channel:{uuid}:metadata"] = {"channel_name": f"CH {uuid}"}
        self.exp[f"live:channel:{uuid}:metadata"] = self.clock.wall() + ttl
        self.sets[f"live:channel:{uuid}:clients"] = {f"c{i}" for i in range(clients)}
        self.exp[f"live:channel:{uuid}:clients"] = self.clock.wall() + 60.0

    def set_clients(self, uuid, clients):
        self.sets[f"live:channel:{uuid}:clients"] = {f"c{i}" for i in range(clients)}
        self.exp[f"live:channel:{uuid}:clients"] = self.clock.wall() + 60.0

    def close_channel(self, uuid):
        self.delete(f"live:channel:{uuid}:metadata")
        self.delete(f"live:channel:{uuid}:clients")


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def fake_redis(fake_clock):
    return FakeRedis(clock=fake_clock)
