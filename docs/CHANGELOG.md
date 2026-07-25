# Changelog

## Unreleased

### Fixed

- **The scheduled report never ran, and every action click re-broke it.**
  `metricsarr_build_report` was found with `queue=None`, `total_run_count=0`,
  `last_run_at=None`. `build_report_task` registers on the `dvr` worker ONLY, so a
  row with no queue routes to the default worker, which rejects every dispatch
  (bug-075). The cause: `run()` calls `sync_schedule` on EVERY action, and
  `create_or_update_periodic_task` has no `queue` parameter, so the hand-applied
  `queue='dvr'` was destroyed by the next Validate/Build/Summary press.
  `sync_schedule` now re-asserts the queue, and self-heals from `queue=None` on the
  next click. The AST mutation guard was NOT loosened -- it gained a narrow
  `(file, receiver-name)` allowance plus five counter-fixtures proving it cannot
  widen.
- **A refused notify was recorded as if it had been delivered.** `emit_gate`
  ignored `notify_fn`'s return, and `notify()` never raises -- it returns False. So
  a critical "usage sensor not trustworthy" that was never spooled still recorded
  `prev_ok=False`, and a later recovery emitted "trustworthy again" for a problem
  the operator was never told about. State now advances only when the emit landed,
  so a refusal retries on the next run.
- `_emit_notifications` no longer discards `emit_report`'s bool; it returns
  `{"enabled", "report_emitted", "error"}`, error redacted.

### Changed

- **Removed the built-in Discord/generic-JSON webhook** (`webhook.py`,
  `webhook_url`/`webhook_format` settings, the `send_webhook_now` action).
  Replaced with a single **`notify_enabled`** toggle that hands the report
  summary and honesty-gate alerts to the Newsflasharr plugin, if installed and
  enabled, keyed on the source name `metricsarr`. Routing (which channel, by
  what severity, quiet hours, storm dedup) now lives entirely in Newsflasharr's
  own settings rather than in a plugin-specific webhook URL/format pair — see
  the README's "Notifications via Newsflasharr" section. The three
  credential-scrubbing regexes and `redact()` moved out of `webhook.py` into
  their own module, `redaction.py`, unchanged in behavior. `reports.
  summary_for_webhook()` is renamed `summary_for_notify()` (same shape).
  Operators who relied on the built-in webhook need to install Newsflasharr and
  add a routing rule pointed at their preferred channel; `report_base_url` is
  unchanged and still makes the report link in that notification clickable.

## v1.26.2011347 (July 20, 2026)

Deployed + verified live on Dispatcharr 0.28.0 on 2026-07-20. 334 tests.

### Fixed

- **A green report run no longer outlives a failed publish (bug-078).** The
  report's counts are computed *before* the write, and `write_report()` never
  raises (it degrades by design, returning `html_path=None` plus an error
  string) — but neither caller inspected that signal. `_build_report()`
  hardcoded `status="ok"` and merely appended the error in parentheses, and
  `build_report_task()` discarded the write result entirely and returned the
  counts, which Celery records as SUCCESS like any other return value. The
  first-ever scheduled run hit exactly this: `/data/logos/metricsarr` and
  `/config/metricsarr` had been created `root:root` by an earlier `docker exec`
  (which defaults to root) while the Celery worker runs as `dispatch`, so every
  write raised `PermissionError` and the task reported a full, healthy-looking
  set of counts while publishing nothing. The only evidence was `report.html`'s
  mtime never moving.

  The action's status now derives from whether the HTML was actually published,
  and the scheduled task raises when it wasn't — inside the existing handler, so
  the message is still redacted and re-raised `from None`, preserving both the
  credential guarantee and Celery's failure/retry semantics. The webhook now
  fires only after a confirmed publish, since its summary links to the report.

  A CSV-only failure deliberately remains a degraded success: the
  nginx-served HTML report is the product, the CSV is a convenience export to a
  bind mount. A regression test pins that asymmetry so a future tightening
  can't quietly turn it into a hard failure.

  This is the same shape as the Task-10 leftover fixed in v1.26.1941407 (a
  returned error value read as SUCCESS), one layer down: there an exception was
  swallowed into a return value, here no exception was ever created. **The
  general rule: a green Celery result proves the task returned, not that it
  accomplished anything — assert on the artifact.**

## v1.26.1941407 — Phase 1 (July 14, 2026)

First release. Merged to `master` and **deployed + verified live** on the
production Dispatcharr container (0.27.2) on 2026-07-14: collector elected
leader and renews its lease, `usage.json` written with `stats_since` and a
clean self-health block (zero Redis errors), `build_report` runs end-to-end,
the report is reachable at `http://<host>:9191/logos/metricsarr/report.html`,
the Beat row `metricsarr_build_report` is registered, and the live channel
count was byte-for-byte unchanged across the deploy (read-only confirmed on
the real box, not just by the AST guard). 331 tests. Not yet released to the
Dispatcharr Hub (no GitHub remote / tag / Hub PR).

The **Fixed** and **Improved** sections below are pre-release hardening driven
by adversarial per-task and whole-branch review during development — not
changes to previously-shipped behavior. They are recorded because several were
defects in the design/plan (a clock-jump test that forced watch-qualification
onto raw wall-clock time and silently discarded genuine watches; a `shutdown()`
path that let a non-leader wipe `usage.json`; a never-watched ceiling that was
mathematically unreachable on a real lineup), and the history is worth keeping.

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
