"""Structural proof that Phase 1 cannot mutate Dispatcharr (spec S11.1).

A guard nothing routes through is not a guard: this test reads the AST of every
shipped module and fails if a write-shaped Django ORM call, or any
subprocess/provider I/O, exists ANYWHERE.

This guard targets DJANGO ORM WRITES SPECIFICALLY, not method names in the
abstract. A name-only ban (flag any `.save()/.delete()/.update()/...` call
regardless of receiver) sounds stricter but is actually WRONG against this
codebase and would false-positive on legitimate, non-ORM code:

    collector.py:  self.r.delete(self.key)         -- Redis, not the ORM
    sessionizer.py: merged.update(thresholds or {}) -- dict.update
    webhook.py:     body.update(data)               -- dict.update
    reports.py:     seen.add(entry["uuid"])         -- set.add
    reports.py/storage.py: os.remove(tmp)           -- filesystem, not ORM

So the detector below only flags a call when the receiver PROVES a Django
queryset/model is on the other end:

  * the attribute-access chain leading to the call contains `.objects`
    anywhere (e.g. `Channel.objects.filter(...).update(...)`), OR
  * the chain bottoms out at a known Dispatcharr model class name
    (`Channel`, `Stream`, `M3UAccount`, ...), OR
  * the method itself has NO stdlib/dict/set/list/Redis-client collision at
    all (`save`, `bulk_create`, `bulk_update`, `get_or_create`,
    `update_or_create`) -- these are ORM-only names, so they are flagged
    unconditionally, matching the brief's `channel.save()` example.

Two exceptions this guard must (and does, by construction) permit:
`core.scheduling.create_or_update_periodic_task(...)` and
`delete_periodic_task(...)` in plugin.py, which write Celery Beat schedule
rows, not Dispatcharr channel data, and are explicitly blessed by the spec.
They are bare function-name calls (`ast.Name`, imported via
`from core.scheduling import ...`), never attribute calls on a model, so the
detector -- which only ever inspects `ast.Attribute` call targets -- never
even looks at them. Proven below by both the real-file scan (plugin.py is in
SHIPPED and passes) and a synthetic self-test.

The self-tests below prove the guard fires on true ORM writes AND stays
silent on the legitimate patterns actually shipped in this repo -- a guard
nothing routes through is not a guard.
"""
import ast

import pytest
from conftest import PLUGIN_DIR

SHIPPED = sorted(p for p in PLUGIN_DIR.glob("*.py"))

# Known Dispatcharr ORM model names reachable from this plugin (gateway.py
# imports Channel today; the others are listed defensively for any future
# module, per the controller resolution).
KNOWN_MODELS = {
    "Channel", "ChannelGroup", "ChannelProfile", "ChannelProfileMembership",
    "ChannelStream", "Stream", "M3UAccount", "M3UAccountProfile", "Recording",
    "EPGData", "EPGSource", "PluginConfig",
}

# Write-shaped method names that ALSO exist on plain dicts/sets/lists and the
# duck-typed Redis client this plugin polls -- flag only when the receiver
# chain proves a Django queryset/model is on the other end.
AMBIGUOUS_ORM_METHODS = {"delete", "create", "update", "add", "remove",
                         "set_hidden"}

# Write-shaped method names with no stdlib/dict/set/list/Redis collision --
# any call to one of these is unambiguously a Django ORM write.
UNAMBIGUOUS_ORM_METHODS = {"save", "bulk_create", "bulk_update",
                           "get_or_create", "update_or_create"}

FORBIDDEN_ORM_METHODS = AMBIGUOUS_ORM_METHODS | UNAMBIGUOUS_ORM_METHODS

# Anything that could reach the provider. One ffprobe kicks a live viewer.
# `webhook.py`'s `urllib` POST to the user's OWN endpoint is untouched by
# this list -- urllib is not subprocess, and it isn't provider I/O.
FORBIDDEN_IO_NAMES = {"ffprobe", "Popen", "check_output", "check_call", "system"}
FORBIDDEN_IO_MODULES = ("subprocess",)


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _receiver_chain_tokens(node):
    """Walk the receiver expression of `X.method(...)` (i.e. `X`) through
    nested Attribute/Call/Subscript wrappers, collecting every attribute name
    plus the root Name id. `Channel.objects.filter(id=1)` -> the receiver of
    `.filter` is `Channel.objects`, whose tokens are ["objects", "Channel"].
    """
    tokens = []
    while True:
        if isinstance(node, ast.Attribute):
            tokens.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            tokens.append(node.id)
            break
        else:
            break
    return tokens


def _is_orm_receiver(base):
    tokens = _receiver_chain_tokens(base)
    return "objects" in tokens or any(t in KNOWN_MODELS for t in tokens)


def _orm_write_offenders(source, filename="<test>"):
    """Return a list of "<file>:<line> .<method>()" strings for every
    write-shaped Django ORM call found in `source`. Bare Name calls (e.g.
    `create_or_update_periodic_task(...)`) are never examined -- only
    attribute calls (`X.method(...)`) are ORM-shaped at all."""
    tree = ast.parse(source, filename=filename)
    offenders = []
    for call in _calls(tree):
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in UNAMBIGUOUS_ORM_METHODS:
            offenders.append(f"{filename}:{call.lineno} .{func.attr}()")
        elif func.attr in AMBIGUOUS_ORM_METHODS and _is_orm_receiver(func.value):
            offenders.append(f"{filename}:{call.lineno} .{func.attr}()")
    return offenders


def _io_offenders(source, filename="<test>"):
    tree = ast.parse(source, filename=filename)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_IO_MODULES:
                    offenders.append(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in FORBIDDEN_IO_MODULES:
                offenders.append(
                    f"{filename}:{node.lineno} from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in FORBIDDEN_IO_NAMES:
                offenders.append(f"{filename}:{node.lineno} {name}()")
    return offenders


# ---------------------------------------------------------------------------
# The real guard: scan every shipped module.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_no_orm_writes_anywhere_in_the_plugin(path):
    offenders = _orm_write_offenders(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, (
        "Phase 1 must never mutate Dispatcharr. Found write-shaped ORM calls: "
        + ", ".join(offenders))


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_no_subprocess_or_provider_io(path):
    offenders = _io_offenders(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, (
        "No provider I/O, ever -- one probe kicks a live viewer. Found: "
        + ", ".join(offenders))


def test_collector_modules_are_stdlib_only():
    """The collector must import with no Django present: it runs in a thread that
    must never touch the ORM (spec S11.2)."""
    banned = ("django", "apps.", "celery")
    for name in ("collector", "sessionizer", "storage", "gates", "webhook"):
        source = (PLUGIN_DIR / f"{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                assert not any(mod.startswith(b) for b in banned), \
                    f"{name}.py imports {mod} -- it must stay stdlib-only"


# ---------------------------------------------------------------------------
# Self-tests: prove the guard is precise, both directions. A guard nothing
# routes through is not a guard -- these snippets are the routing proof.
# ---------------------------------------------------------------------------

TRUE_POSITIVE_ORM_WRITES = {
    "queryset_update": 'Channel.objects.filter(id=1).update(name="x")',
    "instance_save": "channel.save()",
    "queryset_create": 'Channel.objects.create(name="x")',
    "get_or_create": 'ChannelGroup.objects.get_or_create(name="x")',
    "queryset_delete_via_all": "Stream.objects.all().delete()",
    "bulk_update": 'Channel.objects.bulk_update(items, ["name"])',
    "bulk_create": "M3UAccount.objects.bulk_create(rows)",
    "update_or_create": "M3UAccount.objects.update_or_create(id=1, defaults={})",
}


@pytest.mark.parametrize("snippet", TRUE_POSITIVE_ORM_WRITES.values(),
                         ids=TRUE_POSITIVE_ORM_WRITES.keys())
def test_guard_fires_on_true_positive_orm_writes(snippet):
    offenders = _orm_write_offenders(snippet, "<synthetic>")
    assert offenders, f"guard failed to flag a real ORM write: {snippet!r}"


LEGITIMATE_NON_ORM_PATTERNS = {
    # Verbatim from the shipped modules (see the module docstring).
    "redis_delete": "self.r.delete(self.key)",
    "dict_update_sessionizer": "merged.update(thresholds or {})",
    "dict_update_webhook": "body.update(data)",
    "set_add_reports": 'seen.add(entry["uuid"])',
    "os_remove": "os.remove(tmp)",
    # Generalizations of the same shapes, not literally shipped but the same
    # class of false positive a name-only ban would produce.
    "list_remove": "items.remove(x)",
    "dict_pop": 'record.pop("key", None)',
    # The two spec-blessed exceptions: Celery Beat schedule helpers. These
    # are bare Name calls (imported via `from core.scheduling import ...`),
    # never attribute calls on a model -- confirm the guard leaves them alone
    # even though "update"/"periodic" appear in the function's own name.
    "beat_create_or_update": (
        'create_or_update_periodic_task("metricsarr_build_report", '
        '"tasks.build_report", cron_expression="0 3 * * *", enabled=True)'),
    "beat_delete": 'delete_periodic_task("metricsarr_build_report")',
}


@pytest.mark.parametrize("snippet", LEGITIMATE_NON_ORM_PATTERNS.values(),
                         ids=LEGITIMATE_NON_ORM_PATTERNS.keys())
def test_guard_does_not_fire_on_legitimate_patterns(snippet):
    offenders = _orm_write_offenders(snippet, "<synthetic>")
    assert not offenders, (
        f"guard false-positived on legitimate non-ORM code: {snippet!r} -> "
        + ", ".join(offenders))


# NOTE: every value below is a plain string literal fed to ast.parse() by the
# test -- it is source TEXT the guard analyzes, never code this suite (or
# anything else) executes. In particular "os.system(...)" here is inert test
# fixture data, not a real subprocess/shell invocation.
TRUE_POSITIVE_PROVIDER_IO = {
    "subprocess_popen": "subprocess.Popen(['ffprobe', url])",
    "subprocess_check_output": "subprocess.check_output(['ffprobe', url])",
    "os_system": "os.system('ffprobe ' + url)",
    "bare_ffprobe_call": "ffprobe(url)",
    "subprocess_import": "import subprocess",
}


@pytest.mark.parametrize("snippet", TRUE_POSITIVE_PROVIDER_IO.values(),
                         ids=TRUE_POSITIVE_PROVIDER_IO.keys())
def test_io_guard_fires_on_true_positive_provider_io(snippet):
    offenders = _io_offenders(snippet, "<synthetic>")
    assert offenders, f"I/O guard failed to flag: {snippet!r}"


LEGITIMATE_IO_PATTERNS = {
    # webhook.py's actual pattern: POST to the USER'S OWN endpoint, not the
    # provider. Must never be confused with provider I/O.
    "urllib_post": (
        "urllib.request.urlopen(urllib.request.Request(url, data=payload, "
        "method='POST'))"),
    "os_replace": "os.replace(tmp, path)",
    "os_makedirs": "os.makedirs(data_dir, exist_ok=True)",
}


@pytest.mark.parametrize("snippet", LEGITIMATE_IO_PATTERNS.values(),
                         ids=LEGITIMATE_IO_PATTERNS.keys())
def test_io_guard_does_not_fire_on_legitimate_io(snippet):
    offenders = _io_offenders(snippet, "<synthetic>")
    assert not offenders, (
        f"I/O guard false-positived: {snippet!r} -> " + ", ".join(offenders))
