"""Shared fixtures for Dustarr tests.

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

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "dustarr"
_PKG = "dustarr_under_test"


def _install_runtime_stubs():
    apps = types.ModuleType("apps")
    apps.__path__ = []
    apps_channels = types.ModuleType("apps.channels")
    apps_channels.__path__ = []
    models = types.ModuleType("apps.channels.models")
    for name in ("Channel", "ChannelGroup", "ChannelProfile",
                 "ChannelProfileMembership", "StreamProfile"):
        setattr(models, name, MagicMock(name=name))
    # plugin._load_settings distinguishes PluginConfig.DoesNotExist (a missing
    # row, legitimately {}) from every other failure (which must propagate).
    # DoesNotExist must be a real exception CLASS: an auto-created MagicMock
    # attribute is not a BaseException subclass and would break the except
    # clause itself.
    apps_plugins = types.ModuleType("apps.plugins")
    apps_plugins.__path__ = []
    plugins_models = types.ModuleType("apps.plugins.models")
    plugins_models.PluginConfig = MagicMock(name="PluginConfig")
    plugins_models.PluginConfig.DoesNotExist = type(
        "DoesNotExist", (Exception,), {})
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
    # I3: gateway._default_stream_profile_name() resolves the global default
    # stream profile via core.models.CoreSettings -- stubbed here so the
    # function-local import inside gateway.py resolves in tests.
    core_models = types.ModuleType("core.models")
    core_models.CoreSettings = MagicMock(name="CoreSettings")
    # sync_schedule re-asserts `queue='dvr'` on dustarr's OWN Beat row
    # (bug-075; the 2026-07-25 outage), reaching django_celery_beat through
    # plugin._periodic_task_qs(). Stubbed here rather than monkeypatched per
    # test, so the REAL code path runs under test instead of being bypassed.
    dcb = types.ModuleType("django_celery_beat")
    dcb.__path__ = []
    dcb_models = types.ModuleType("django_celery_beat.models")
    dcb_models.PeriodicTask = MagicMock(name="PeriodicTask")
    sys.modules.update({
        "apps": apps, "apps.channels": apps_channels,
        "apps.channels.models": models,
        "apps.plugins": apps_plugins, "apps.plugins.models": plugins_models,
        "django": django, "django.db": django_db,
        "core": core, "core.utils": core_utils, "core.scheduling": core_sched,
        "core.models": core_models,
        "django_celery_beat": dcb, "django_celery_beat.models": dcb_models,
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
    """Load a stdlib-only module (sessionizer/storage/collector/gates/redaction)."""
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
                "redaction", "reports", "notify_client", "notify_report",
                "plugin"):
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
    """Dict-backed stub of the Redis commands Dustarr uses."""

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


# ---------------------------------------------------------------------------
# Shared report-rendering sample model (FIX 8).
#
# test_report_sections.py and test_report_render.py used to each carry their
# own byte-identical copy of NOW/SETTINGS/model()/the rp+gw fixtures.
# scripts/render_sample.py (the committed-fixture regenerator) reached into
# test_report_sections.py to borrow its model() -- so renaming that test's
# helper would silently break the regenerator with no test failure pointing
# at the real cause. Sharing one copy here means both the tests and the
# regenerator use the same sample data by construction.
# ---------------------------------------------------------------------------

NOW = 1_700_000_000.0


def _sessionizer_bucket_key(ts):
    """The UTC hour-bucket key the collector writes coverage under.

    Imported lazily from the module under test rather than reimplemented, so a
    change to the bucket format cannot leave this helper silently building keys
    that never match.
    """
    return sys.modules["dustarr_under_test.sessionizer"].bucket_key(ts)

SETTINGS = {"exclude_groups": "", "exclude_name_regex": "",
            "exclude_auto_created": False, "top_n": 5,
            "unused_threshold_days": 30, "never_watched_ceiling": 0.99,
            "poll_interval_s": 15.0, "client_gap_grace_s": 90.0}


def model(rp, gw, n=5, watched=2):
    """Sample channel-usage model used by the report-rendering tests and by
    scripts/render_sample.py (the fixture regenerator)."""
    rows = [gw.ChannelRow(id=i, uuid=f"u{i}", name=f"CH{i}", group="US: Movies",
                          auto_created=False, created_at=NOW - 90 * 86400,
                          proxying=True) for i in range(n)]
    channels = {f"u{i}": {"watch_count": 3, "watch_seconds": 7200.0,
                          "tune_count": 3, "last_watched": NOW - 3600,
                          "last_tuned": NOW - 3600,
                          "first_seen": NOW - 80 * 86400}
                for i in range(watched)}
    usage = {"channels": channels,
             "meta": {"stats_since": NOW - 40 * 86400, "coverage": {}}}
    return rp.build_model(rows, usage, SETTINGS, NOW)


# The published-report counter carried by the committed fixture. A real run
# reads this from /data/dustarr/report_count.json; the fixture has no such
# file, so the number is supplied here. It is not zero, because a zero renders
# no chip at all and the fixture would then stop covering the masthead chip.
SAMPLE_REPORT_NUMBER = 37

SAMPLE_SETTINGS = dict(SETTINGS, top_n=5, exclude_groups="US: Local News",
                       unused_threshold_days=30)


def _full_coverage(now, window_days=40, poll_interval_s=15.0):
    """Coverage buckets for a collector that sampled the whole window.

    gates.coverage_fraction counts an hour as covered only when it holds at
    least 90% of the ticks the poll interval implies AND its largest gap fits
    inside the client grace, so both numbers have to be filled in: a bucket
    carrying only a tick count scores zero coverage.
    """
    ticks = int(3600.0 / poll_interval_s)
    return {_sessionizer_bucket_key(now - hour * 3600):
            {"ticks": ticks, "max_gap_s": poll_interval_s}
            for hour in range(int(window_days * 24))}


def sample_model(rp, gw):
    """The exact model behind tests/fixtures/sample_report.html.

    Defined once and used by BOTH scripts/render_sample.py (which writes the
    fixture) and the test that compares against it. When those two built their
    model separately, adding a single key to one of them broke the comparison
    with no defect in the renderer at all.

    Deliberately RICHER than `model()` above, and deliberately healthy. Every
    section of the report has entries here -- never watched, going cold, still
    being tried, too new to judge, tuned but never qualified, most used, least
    used, excluded and unobservable -- across several channel groups, so the
    fixture exercises the table, chart and rollup markup rather than a page of
    empty headings. Coverage is complete and enough distinct channels were
    watched to clear the honesty gates, because a fixture that always carries
    the "not trustworthy" banner cannot show what a normal report looks like.
    The banner itself is covered by its own test, which builds a blind dataset.
    """
    day = 86400.0
    rows, channels = [], []

    def add(uuid, name, group, *, age_days=200, proxying=True,
            auto_created=False, record=None):
        rows.append(gw.ChannelRow(id=len(rows) + 1, uuid=uuid, name=name,
                                  group=group, auto_created=auto_created,
                                  created_at=NOW - age_days * day,
                                  proxying=proxying))
        if record is not None:
            channels.append((uuid, record))

    def watched(count, hours, last_days, tunes=None):
        return {"watch_count": count, "watch_seconds": hours * 3600.0,
                "tune_count": tunes if tunes is not None else count,
                "last_watched": NOW - last_days * day,
                "last_tuned": NOW - last_days * day,
                "first_seen": NOW - 180 * day}

    # Watched regularly: these carry the most-used and least-used rankings.
    add("u-news24", "BBC News HD", "UK: News", record=watched(48, 61.0, 0.4))
    add("u-film1", "Paramount Network", "US: Movies", record=watched(31, 44.5, 1.2))
    add("u-doc", "Smithsonian Channel", "US: Documentary", record=watched(22, 29.0, 2.1))
    add("u-kids", "Cartoon Network", "US: Kids", record=watched(17, 12.5, 0.8))
    add("u-food", "Food Network", "US: Lifestyle", record=watched(9, 6.0, 3.5))
    add("u-hist", "History Channel", "US: Documentary", record=watched(4, 2.5, 5.0))

    # Watched once, long ago: the "going cold" list, the retirement queue.
    add("u-cold1", "Travel Channel", "US: Lifestyle", record=watched(2, 1.5, 41.0))
    add("u-cold2", "Discovery Turbo", "US: Documentary", record=watched(1, 0.6, 55.0))

    # Cold by watching but tuned recently: somebody is still trying, which
    # reads as broken rather than unwanted, so it is listed apart.
    still_tried = watched(1, 0.5, 47.0)
    still_tried["last_tuned"] = NOW - 2 * day
    still_tried["tune_count"] = 14
    add("u-tried", "Sky Sports Main Event", "UK: Sports", record=still_tried)

    # Tuned repeatedly, never long enough to count as a watch: almost certainly
    # a broken source rather than an unpopular channel.
    for uuid, name, group, tunes in (
            ("u-brk1", "AMC HD", "US: Movies", 11),
            ("u-brk2", "TLC", "US: Lifestyle", 6)):
        add(uuid, name, group,
            record={"watch_count": 0, "watch_seconds": 0.0,
                    "tune_count": tunes, "last_watched": None,
                    "last_tuned": NOW - 1.5 * day,
                    "first_seen": NOW - 120 * day})

    # Never watched: the dead weight the report exists to find.
    for uuid, name, group in (
            ("u-dead1", "Shopping HQ", "US: Shopping"),
            ("u-dead2", "Gems TV", "US: Shopping"),
            ("u-dead3", "Poker Central", "US: Sports"),
            ("u-dead4", "QVC Style", "US: Shopping"),
            ("u-dead5", "Baby First TV", "US: Kids"),
            ("u-dead6", "Classic Arts Showcase", "US: Movies"),
            ("u-dead7", "Motorvision TV", "US: Documentary")):
        add(uuid, name, group)

    # Created inside the unused threshold: not dead weight, just too new to
    # judge fairly.
    add("u-new1", "Fox Weather", "US: News", age_days=9)
    add("u-new2", "Nosey", "US: Lifestyle", age_days=21)

    # Held back from judgment: an excluded group, and a channel whose stream
    # profile is not proxying so the collector cannot see it at all.
    add("u-exc1", "WKYC Cleveland", "US: Local News")
    add("u-exc2", "WEWS Cleveland", "US: Local News")
    add("u-unobs", "Willow Cricket", "US: Sports", proxying=False)

    usage = {"channels": dict(channels),
             "meta": {"stats_since": NOW - 40 * day,
                      "coverage": _full_coverage(NOW)}}
    m = rp.build_model(rows, usage, SAMPLE_SETTINGS, NOW)
    m["report_number"] = SAMPLE_REPORT_NUMBER
    return m


@pytest.fixture()
def rp():
    return sys.modules["dustarr_under_test.reports"]


@pytest.fixture()
def gw():
    return sys.modules["dustarr_under_test.gateway"]
