"""Structural proof that Phase 1 cannot mutate Dispatcharr (spec S11.1).

A guard nothing routes through is not a guard: this test reads the AST of every
shipped module and fails if a write-shaped Django ORM call, or any
subprocess/provider I/O, exists ANYWHERE.

This guard targets DJANGO ORM WRITES SPECIFICALLY, not method names in the
abstract. A name-only ban (flag any `.save()/.delete()/.update()/...` call
regardless of receiver) sounds stricter but is actually WRONG against this
codebase and would false-positive on legitimate, non-ORM code:

    collector.py:  self.r.delete(self.key)         -- Redis, not the ORM
    collector.py:  self.r.set(self.key, ...)        -- Redis, not the ORM
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
  * a Name (or `self.attr` dotted path, or a for-loop target, or a
    helper function's own name) was ever bound ANYWHERE ELSE in the module
    from an ORM-proven expression -- an ASSIGNMENT-PROVENANCE pass, so
    `qs = Channel.objects.filter(...)` then `qs.update(...)` (including
    through an aliased import, a list comprehension, or a for-loop target
    like `for channel in queryset.iterator(): channel.streams.add(...)`)
    is caught even though the call site itself never mentions `.objects`, OR
  * the method itself has NO stdlib/dict/set/list/Redis-client collision at
    all (`save`, `bulk_create`, `bulk_update`, `get_or_create`,
    `update_or_create`, and the Django 4.1+ async names `asave`, `adelete`,
    `acreate`, `aupdate`) -- these are ORM-only names, so they are flagged
    unconditionally, matching the brief's `channel.save()` example.

`.delete()` is handled differently again: it is RECEIVER-AGNOSTIC (flagged by
default) with a single narrow safe-receiver exception (`self.r` -- the Redis
client `collector.py` actually calls `.delete()` on). A guard whose entire
job is proving a negative must default to FAIL on an unprovable receiver, not
PASS -- `channel.delete()` (an instance delete with no `.objects` in sight)
is exactly the shape a future author would reach for first, and the old
receiver-proof-required design let it straight through.

Raw SQL (`cursor.execute("UPDATE ...")`) is flagged whenever `.execute()` is
called on a receiver whose chain mentions "cursor" -- this deliberately
excludes `pipe.execute()` (the Redis pipeline `collector.py` actually uses),
whose receiver chain never does.

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

SHIPPED = sorted(p for p in PLUGIN_DIR.rglob("*.py"))

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
# chain proves a Django queryset/model is on the other end (structurally, or
# via the assignment-provenance pass below). `clear`/`set` are Django's other
# m2m-related-manager writers (`channel.streams.clear()`, `.set([...])`);
# `set` also collides with `self.r.set(...)` (Redis), hence the gating.
AMBIGUOUS_ORM_METHODS = {"create", "update", "add", "remove", "set_hidden",
                         "clear", "set"}

# Write-shaped method names with no stdlib/dict/set/list/Redis collision --
# any call to one of these is unambiguously a Django ORM write. Includes the
# Django 4.1+ async ORM names, which have no collision either.
UNAMBIGUOUS_ORM_METHODS = {"save", "bulk_create", "bulk_update",
                           "get_or_create", "update_or_create",
                           "asave", "adelete", "acreate", "aupdate"}

# `.delete()` is receiver-AGNOSTIC (flagged unless the receiver is on this
# narrow allowlist) rather than receiver-gated like the ambiguous set above --
# see the module docstring. The only legitimate `.delete()` anywhere in this
# codebase is the Redis client `collector.py` stores as `self.r`.
SAFE_DELETE_RECEIVERS = {"self.r"}

FORBIDDEN_ORM_METHODS = AMBIGUOUS_ORM_METHODS | UNAMBIGUOUS_ORM_METHODS | {"delete"}

# Anything that could reach the provider. One ffprobe kicks a live viewer.
# `webhook.py`'s `urllib` POST to the user's OWN endpoint is untouched by
# this list -- urllib is not subprocess, and it isn't provider I/O.
FORBIDDEN_IO_NAMES = {"ffprobe", "Popen", "check_output", "check_call", "system"}
FORBIDDEN_IO_MODULES = ("subprocess",)

# Same shell-escape risk as `os.system`/`subprocess.Popen` but with a name
# generic enough (`run`, `popen`) that an unqualified ban would risk false
# positives on unrelated `.run()`/`.popen()`-shaped methods elsewhere -- so
# these are gated on the dotted receiver actually being `os`/`subprocess`.
FORBIDDEN_IO_QUALIFIED = {
    ("subprocess", "run"),
    ("os", "popen"),
    ("os", "execv"), ("os", "execl"),
    ("os", "spawnl"), ("os", "spawnv"),
}


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _receiver_chain_tokens(node):
    """Walk the receiver expression of `X.method(...)` (i.e. `X`) through
    nested Attribute/Call/Subscript wrappers, collecting every attribute name,
    every call-target name (so a helper call like `f()` contributes token
    "f", closing the "helper-returned queryset" gap), plus the root Name id.
    `Channel.objects.filter(id=1)` -> the receiver of `.filter` is
    `Channel.objects`, whose tokens are ["objects", "Channel"].
    """
    tokens = []
    while True:
        if isinstance(node, ast.Attribute):
            tokens.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                tokens.append(func.id)
            elif isinstance(func, ast.Attribute):
                tokens.append(func.attr)
            node = func
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            tokens.append(node.id)
            break
        else:
            break
    return tokens


def _dotted(node):
    """Return "a.b.c" for a pure Name/Attribute chain (no Call/Subscript in
    it), else None. Used to key `self.queryset`-shaped assignment targets and
    to check the safe-receiver allowlist for `.delete()`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_orm_receiver(base, orm_names=frozenset(), orm_dotted=frozenset()):
    """True if `base` (the receiver expression of some outer call, OR any
    expression being checked for ORM-provenance during alias collection)
    structurally proves a Django queryset/model, or resolves -- via the
    assignment-provenance pass -- to a Name/dotted-path already proven ORM
    elsewhere in the module."""
    tokens = _receiver_chain_tokens(base)
    if "objects" in tokens or any(t in KNOWN_MODELS for t in tokens):
        return True
    if orm_names and any(t in orm_names for t in tokens):
        return True
    path = _dotted(base)
    return bool(path and path in orm_dotted)


def _collect_orm_aliases(tree):
    """Assignment-provenance pass (single forward scan, order-sensitive --
    "for the rest of the module" per the module docstring): any Name or
    dotted attribute target ever bound from an ORM-proven expression becomes
    an ORM receiver for every later call site. Closes `qs = Channel.objects
    ...` then `qs.update()`/`qs.delete()` (including through an aliased
    import, since the chain still carries `.objects`), `self.queryset = ...`
    then `self.queryset.update()`, `for channel in queryset.iterator():` then
    `channel.streams.add(...)` (m2m related-manager writes), and a helper
    function whose `return` is ORM-shaped, so `f().update()` cannot hide
    behind an otherwise-unprovable call receiver.
    """
    orm_names, orm_dotted = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_orm_receiver(node.value, orm_names, orm_dotted):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    orm_names.add(target.id)
                else:
                    path = _dotted(target)
                    if path:
                        orm_dotted.add(path)
        elif isinstance(node, ast.For) and _is_orm_receiver(node.iter, orm_names, orm_dotted):
            if isinstance(node.target, ast.Name):
                orm_names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None and \
                        _is_orm_receiver(sub.value, orm_names, orm_dotted):
                    orm_names.add(node.name)
                    break
    return orm_names, orm_dotted


def _orm_write_offenders(source, filename="<test>"):
    """Return a list of "<file>:<line> .<method>()" strings for every
    write-shaped Django ORM call found in `source`. Bare Name calls (e.g.
    `create_or_update_periodic_task(...)`) are never examined -- only
    attribute calls (`X.method(...)`) are ORM-shaped at all."""
    tree = ast.parse(source, filename=filename)
    orm_names, orm_dotted = _collect_orm_aliases(tree)
    offenders = []
    for call in _calls(tree):
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        if attr in UNAMBIGUOUS_ORM_METHODS:
            offenders.append(f"{filename}:{call.lineno} .{attr}()")
        elif attr == "delete":
            # Receiver-AGNOSTIC: flagged by default, safe only on the narrow
            # allowlist (the Redis client). No `.objects` proof required to
            # fail -- proof of SAFETY is required to pass.
            if _dotted(func.value) not in SAFE_DELETE_RECEIVERS:
                offenders.append(f"{filename}:{call.lineno} .{attr}()")
        elif attr == "execute":
            if any("cursor" in t.lower() for t in _receiver_chain_tokens(func.value)):
                offenders.append(f"{filename}:{call.lineno} .{attr}() [raw SQL]")
        elif attr in AMBIGUOUS_ORM_METHODS and _is_orm_receiver(func.value, orm_names, orm_dotted):
            offenders.append(f"{filename}:{call.lineno} .{attr}()")
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
            elif (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                  and (func.value.id, func.attr) in FORBIDDEN_IO_QUALIFIED):
                offenders.append(
                    f"{filename}:{node.lineno} {func.value.id}.{func.attr}()")
    return offenders


def _module_scope_statements(tree):
    """Yield statements at true module scope: the Module body, plus the body
    and handlers of any top-level Try (plugin.py wraps its sibling-module
    imports in one), but never descending into a function or class body."""
    for node in tree.body:
        if isinstance(node, ast.Try):
            yield from node.body
            for handler in node.handlers:
                yield from handler.body
        else:
            yield node


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


def test_gateway_and_reports_have_no_module_scope_django_imports():
    """A module-scope Django/ORM/Celery import breaks Dispatcharr's plugin
    loader (which imports plugin.py, and transitively gateway.py/reports.py,
    before Django is necessarily ready) with CI green -- the function-local
    pattern these two modules rely on (spec S11.3) was previously unenforced
    except by convention. gateway.py's `Channel` import lives inside
    `DjangoGateway.channels()`, several indents deep; a future author moving
    it to the top of the file for convenience would pass every other test in
    this suite and still break the loader."""
    banned = ("django", "apps.", "celery")
    for name in ("gateway", "reports"):
        source = (PLUGIN_DIR / f"{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename=f"{name}.py")
        for node in _module_scope_statements(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                assert not any(mod.startswith(b) for b in banned), \
                    f"{name}.py has a MODULE-SCOPE import of {mod} -- breaks the plugin loader"


def test_plugin_py_module_scope_only_allows_celery_shared_task():
    """`from celery import shared_task` is the one Django/ORM/Celery-family
    import plugin.py is allowed at module scope (the @shared_task decorator
    is what registers build_report_task with Celery, C1 -- it cannot be
    function-local). Everything else in that family must stay function-local,
    same as every other module here."""
    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="plugin.py")
    banned = ("django", "apps.")
    for node in _module_scope_statements(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(b) for b in banned) and alias.name != "celery", \
                    f"plugin.py has an unexpected module-scope import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "celery":
                assert {a.name for a in node.names} == {"shared_task"}, (
                    "plugin.py's only module-scope celery import must be "
                    "`from celery import shared_task`")
            else:
                assert not any(mod.startswith(b) for b in banned), \
                    f"plugin.py has a MODULE-SCOPE import of {mod} -- breaks the plugin loader"


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
    "async_asave": "channel.asave()",
    "async_adelete": "channel.adelete()",
    "async_acreate": 'Channel.objects.acreate(name="x")',
    "async_aupdate": 'Channel.objects.filter(id=1).aupdate(name="x")',
    "instance_delete_no_objects_in_sight": "channel.delete()",
    "queryset_local_alias_update": (
        'qs = Channel.objects.filter(id=1)\n'
        'qs.update(name="x")'),
    "queryset_local_alias_delete": (
        'qs = Channel.objects.filter(id=1)\n'
        'qs.delete()'),
    "aliased_import_local_qs_update": (
        'from apps.channels.models import Channel as Chan\n'
        'qs = Chan.objects.filter(id=1)\n'
        'qs.update(name="x")'),
    "queryset_update_inside_comprehension": (
        'qs = Channel.objects.filter(id=1)\n'
        '[qs.update(n=i) for i in range(3)]'),
    "self_dot_queryset_update": (
        'class C:\n'
        '    def __init__(self):\n'
        '        self.queryset = Channel.objects.filter(id=1)\n'
        '    def go(self):\n'
        '        self.queryset.update(name="x")'),
    "m2m_related_manager_add_via_for_loop_target": (
        'queryset = Channel.objects.filter(id=1)\n'
        'for channel in queryset.iterator(chunk_size=500):\n'
        '    channel.streams.add(stream)'),
    "m2m_related_manager_remove": (
        'queryset = Channel.objects.filter(id=1)\n'
        'for channel in queryset.iterator(chunk_size=500):\n'
        '    channel.streams.remove(stream)'),
    "m2m_related_manager_clear": (
        'queryset = Channel.objects.filter(id=1)\n'
        'for channel in queryset.iterator(chunk_size=500):\n'
        '    channel.streams.clear()'),
    "m2m_related_manager_create": (
        'queryset = Channel.objects.filter(id=1)\n'
        'for channel in queryset.iterator(chunk_size=500):\n'
        '    channel.streams.create(name="x")'),
    "m2m_related_manager_set": (
        'queryset = Channel.objects.filter(id=1)\n'
        'for channel in queryset.iterator(chunk_size=500):\n'
        '    channel.streams.set([stream])'),
    "helper_returned_queryset_update": (
        'def f():\n'
        '    return Channel.objects.filter(id=1)\n'
        'f().update(name="x")'),
    "raw_sql_cursor_execute": (
        'cursor.execute("UPDATE channels_channel SET name=%s", [x])'),
}


@pytest.mark.parametrize("snippet", TRUE_POSITIVE_ORM_WRITES.values(),
                         ids=TRUE_POSITIVE_ORM_WRITES.keys())
def test_guard_fires_on_true_positive_orm_writes(snippet):
    offenders = _orm_write_offenders(snippet, "<synthetic>")
    assert offenders, f"guard failed to flag a real ORM write: {snippet!r}"


LEGITIMATE_NON_ORM_PATTERNS = {
    # Verbatim from the shipped modules (see the module docstring).
    "redis_delete": "self.r.delete(self.key)",
    "redis_set": "self.r.set(self.key, self.token, nx=True, ex=self.ttl)",
    "redis_pipeline_execute": "counts = pipe.execute()",
    "dict_update_sessionizer": "merged.update(thresholds or {})",
    "dict_update_webhook": "body.update(data)",
    "set_add_reports": 'seen.add(entry["uuid"])',
    "os_remove": "os.remove(tmp)",
    # Generalizations of the same shapes, not literally shipped but the same
    # class of false positive a name-only ban would produce.
    "list_remove": "items.remove(x)",
    "dict_pop": 'record.pop("key", None)',
    "dict_update_generic": 'stats.update({"a": 1})',
    "dict_clear_generic": "stats.clear()",
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


# ---------------------------------------------------------------------------
# Live-injection proof: mutate an in-memory COPY of the real gateway.py (the
# file never touches disk) with the exact two shapes a reviewer proved slid
# past the old guard, and confirm the fixed detector now fires on both.
# ---------------------------------------------------------------------------

def test_guard_catches_queryset_update_injected_into_a_gateway_copy():
    src = (PLUGIN_DIR / "gateway.py").read_text(encoding="utf-8")
    mutated = src.replace(
        "for channel in queryset.iterator(chunk_size=500):",
        'queryset.update(hidden=True)\n        for channel in queryset.iterator(chunk_size=500):',
        1)
    assert mutated != src, "injection site not found -- gateway.py shape changed?"
    offenders = _orm_write_offenders(mutated, "gateway.py (mutated)")
    assert offenders, "guard failed to catch an injected queryset.update()"


def test_guard_catches_channel_delete_injected_into_a_gateway_copy():
    src = (PLUGIN_DIR / "gateway.py").read_text(encoding="utf-8")
    mutated = src.replace(
        "for channel in queryset.iterator(chunk_size=500):",
        "for channel in queryset.iterator(chunk_size=500):\n            channel.delete()",
        1)
    assert mutated != src, "injection site not found -- gateway.py shape changed?"
    offenders = _orm_write_offenders(mutated, "gateway.py (mutated)")
    assert offenders, "guard failed to catch an injected channel.delete()"


# NOTE: every value below is a plain string literal fed to ast.parse() by the
# test -- it is source TEXT the guard analyzes, never code this suite (or
# anything else) executes. In particular "os.system(...)" here is inert test
# fixture data, not a real subprocess/shell invocation.
TRUE_POSITIVE_PROVIDER_IO = {
    "subprocess_popen": "subprocess.Popen(['ffprobe', url])",
    "subprocess_check_output": "subprocess.check_output(['ffprobe', url])",
    "subprocess_run": "subprocess.run(['ffprobe', url])",
    "os_system": "os.system('ffprobe ' + url)",
    "os_popen": "os.popen('ffprobe ' + url)",
    "os_execv": "os.execv(path, args)",
    "os_execl": "os.execl(path, arg0, arg1)",
    "os_spawnl": "os.spawnl(os.P_WAIT, path, arg0)",
    "os_spawnv": "os.spawnv(os.P_WAIT, path, args)",
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
