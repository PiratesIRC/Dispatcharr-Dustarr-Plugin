# Changelog

## Unreleased

### Fixed

- **Critical: the collector never started.** Nothing but `Plugin.run()` ever
  called `ensure_collector()`, and Dispatcharr's settings-save flow never
  calls `run()` — a user who installed, configured, and walked away collected
  nothing, forever. `Plugin.__init__` now calls `ensure_collector()` itself
  (matching the platform hook: `apps/plugins/apps.py`'s `ready()` instantiates
  the plugin class in every uWSGI worker), gated the same way as before by a
  cheap procfs check so it's a no-op everywhere except a live uWSGI worker,
  and stays I/O-free beyond spawning the daemon thread — no settings are read
  in the constructor itself.
- A running collector now picks up a changed setting instead of polling
  forever at a stale cadence. Thread supersession is keyed on
  `(version, thresholds fingerprint)` instead of version alone, so e.g.
  lowering the poll interval respawns the collector immediately rather than
  silently poisoning `coverage_fraction` into 0.0 forever. Shares the
  existing crash-loop budget with version-bump supersession.
- The never-watched alarm ceiling is now denominated on the *judged*
  population (never-watched + too-new + tuned-but-never-qualified + watched)
  instead of the full channel count, and its default was raised from 0.60 to
  0.98. Denominating on the full lineup gave the fraction a hard ceiling far
  below any sane threshold on a real box (most channels are excluded by
  policy), so the gate could never fire under any failure; rebasing on the
  judged population alone would make a perfectly healthy household (which can
  show 80–90% never-watched among the channels it was ever asked to judge)
  trip a 0.60 ceiling permanently, hence the higher default.
- The default stream profile is now resolved once per report run and used as
  the fallback for a `NULL` `stream_profile` (99.9% of a real lineup), instead
  of unconditionally treating `NULL` as "always proxying". Dispatcharr itself
  resolves `NULL` to the global default at play time, so if that default is
  ever pointed at a non-proxying profile, the affected channels are now
  correctly reported as unobservable instead of silently misreported as
  proxying.
- The webhook/toast "link to the full report" is now a real clickable URL
  when the new `report_base_url` setting is configured — previously it was
  always a bare path, which Discord renders as inert text.
- The collector's tick loop now logs (rate-limited) when an exception escapes
  `run_tick` entirely. Previously such a failure was completely invisible: no
  log line, no `stats["last_error"]`, no usage.json update.
- Re-raising a redacted error from the scheduled report task now uses
  `raise ... from None` instead of `from exc`. `from exc` left the *original*,
  credential-bearing exception reachable as `__cause__`, so Celery's
  stored/logged traceback rendered it verbatim even though the redacted
  message string was clean; `from None` suppresses both explicit (`__cause__`)
  and implicit (`__context__`) chaining.
- The structural read-only guard (`tests/test_no_mutations.py`) now also
  catches a Django ORM write bound via `with ... as`, the walrus operator,
  an annotated assignment, or tuple/list-unpack assignment — not just plain
  assignment and `for` loops — and its raw-SQL detector no longer keys on the
  literal variable name `cursor` (a rename such as
  `with connection.cursor() as cur:`, the Django-docs idiom, used to slide
  straight past it). Re-verified zero false positives on every legitimate
  pattern already shipped in this codebase.

### Improved

- The HTML report now shows a "Last watched" column (channel and group
  tables) — arguably the single highest-value signal for deciding what to
  turn off — and the CSV export now formats `last_watched`/`last_tuned` as
  ISO 8601 instead of a raw epoch float.
- The "Least used" section now explains itself with a one-line note when it's
  empty because every watched channel already fit inside "Most used", instead
  of silently rendering "None."
- `gates.py`'s coverage-mechanism docstring corrected (it previously
  described a mechanism that doesn't exist) and `gates._bucket_key` /
  `reports.EMPTY` now delegate to `sessionizer.bucket_key` /
  `sessionizer._blank_record` instead of duplicating their shape.
- README: documented that a queryset arriving as a function parameter is a
  genuine (not evasive) blind spot of the read-only guard, and that the first
  ~30 days of reports carrying the "not trustworthy" banner, and ~70% of a
  default lineup being excluded from judgment, are both by design.

### Added

- Initial Phase 1 release: read-only channel usage metrics for Dispatcharr.
- Leader-lease Redis collector (one active collector across worker processes),
  with session merging, a client-gap grace that survives player reconnect
  retries, and per-poll duration accumulation that stays safe across clock
  jumps and backward NTP steps.
- Report joining the Dispatcharr channel universe (the ORM) against a sparse
  usage overlay (`usage.json`) — a channel absent from the overlay is
  never-watched, the default and expected state, not an error.
- Self-contained HTML report served at `/logos/metricsarr/report.html` — the
  same host and port as the Dispatcharr UI you already have open
  (Dispatcharr's own nginx static route), so it's one click, no extra
  container, no extra port, and it works from a phone or a TV browser — plus
  a CSV export to `/config/metricsarr/`, and a Discord/generic-JSON webhook
  nudge with the headline numbers and a link to the report.
- The "tuned but never qualified" list: channels tried and abandoned inside
  the minimum-watch window, surfaced separately from never-watched because
  they are almost always broken (dead source, black screen, a provider
  connection kick), not simply unused.
- Coverage-density and watch-plausibility gates: the report refuses to be
  trusted, and says so loudly in a banner, when the collector was blind for
  part of the tracking window rather than quietly reporting that nobody
  watched anything.
- Exclusions (auto-created channels, channel groups, channel-name regex) and
  automatic detection of unobservable channels (non-proxying stream
  profiles, which never write the Redis keys the collector polls).
- Credential redaction: every string that can reach a webhook payload or a
  logged error is redacted before it leaves the plugin, and the report only
  ever renders an allowlisted set of per-channel fields.
- Scheduled reporting via Celery Beat (off / daily / weekly / monthly), and
  plugin actions to build the report, show a quick summary, send the webhook
  on demand, and validate settings.
- Structural read-only guard (`tests/test_no_mutations.py`): the build fails
  on any write-shaped Django ORM call it can prove — `.save()`,
  `.bulk_create()`, `.bulk_update()`, `.get_or_create()`,
  `.update_or_create()`, and the async ORM equivalents are flagged
  unconditionally; `.update()`/`.create()`/`.add()`/`.remove()`/`.set()`/
  `.clear()` are flagged once an assignment-provenance pass proves the
  receiver is a Dispatcharr model or queryset (a local alias, a `self.attr`,
  a for-loop variable, or a helper function's return value all count, not
  just a literal `Channel.objects...` at the call site); and `.delete()` is
  flagged by default on ANY receiver, with a single narrow exception for the
  plugin's own Redis client — a guard whose job is proving a negative must
  default to "fail", not "pass", on a receiver it cannot classify. The same
  test fails the build on any subprocess/`os.system`/`os.popen`/`os.exec*`/
  `os.spawn*` or bare `ffprobe` call. It does not and cannot see through
  reflection (`getattr(obj, "delete")()`), `eval`/`exec`, or a write issued
  through a driver this guard doesn't know to look for. The Redis-facing
  modules (`collector`, `sessionizer`, `storage`, `gates`, `webhook`) are
  additionally verified to import no Django, ORM, or Celery code at all, and
  `gateway.py`/`reports.py`/`plugin.py` are verified to keep every
  Django/ORM/Celery import function-local (module-scope imports break the
  plugin loader) except the one `from celery import shared_task` that
  `@shared_task` requires.
