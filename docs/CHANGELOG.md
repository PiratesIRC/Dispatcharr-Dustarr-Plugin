# Changelog

## v1.26.2082008 (July 27, 2026)

Report presentation, and the rename went live.

- **Every section now starts collapsed.** `Never watched`, `Tuned but never
  qualified` and `Most used` were open by default; all six are closed, so the
  page opens as an index rather than a wall of tables.
- **Every section carries a short description** saying what it holds and what
  to do about it. Four had none. The two notes that could have served as one
  (`rankings_note`, `least_used_note`) are CONDITIONAL and render only on some
  boxes, so they can never be relied on for this; the test builds a model where
  neither fires.
- **Rendered copy carries no em dashes, en dashes, double hyphens or
  contractions.** Four places used `--` where prose wanted a full stop: the
  untrustworthy banner, the find-in-page hint, the rankings note and the
  too-new description. In rendered text a `--` reads as an em dash.
- Both rules are bound by tests in `tests/test_report_sections.py`, and both
  were mutation-checked rather than trusted on a first pass.
- `plugin.TASK_PATH` replaces the inline Celery task-path literal, bound by a
  test to the real package directory name. It was the one renamed string
  nothing asserted on, and a wrong path fails invisibly.

## v1.26.2081551 (July 27, 2026)

Renamed the plugin from Metricsarr to Dustarr. The old name promised analytics;
this plugin answers one question, which is what can safely be turned off, and
the name now says so.

- Plugin id `metricsarr` -> `dustarr`, display name `Metricsarr` -> `Dustarr`.
- Paths moved: `/data/metricsarr` -> `/data/dustarr`,
  `/data/logos/metricsarr` -> `/data/logos/dustarr`,
  `/config/metricsarr` -> `/config/dustarr`. The report URL is now
  `http://<host>:9191/logos/dustarr/report.html`.
- Celery task `metricsarr_build_report` -> `dustarr_build_report`, task path
  `_dispatcharr_plugin_dustarr.plugin.build_report_task`. The stale Beat row is
  deleted at migration; `sync_schedule` creates by task name and would
  otherwise leave it dispatching a task that no longer exists.
- Redis leader key `metricsarr:leader` -> `dustarr:leader`.
- Newsflasharr `source` string `metricsarr` -> `dustarr`. A matching routing
  rule must exist on the Newsflasharr side or the report email is silently
  unrouted.
- New `tests/test_name_hygiene.py` fails the build if the legacy name reappears
  in shipped code, tests or tooling.

## v1.26.2071812 (July 26, 2026)

### Added

- **The usage rankings now say when they are omitting real viewing.** "Most
  used" and "Least used" are drawn from the JUDGED population only, so a
  channel that is genuinely watched but sits in an excluded group never
  appears in them. On the reference box that silently hid **21 of 65** watched
  channels, Fox News and the local OTA affiliates among them.
  That is the exclusions working as designed, and it is correct for the
  question this report exists to answer. But omitting a third of real viewing
  with no acknowledgement makes "Most used" read as *what I watch most* when it
  only ever meant *what I watch most among the channels I might turn off*.
  Both sections now carry a one-line note naming the count and the reason.
  Suppressed when the count is zero: a permanent parenthetical nobody needs is
  how real notices get tuned out.
  Computed in `render_html` from `model["excluded"]`, so there is no model
  change and the `counts` sum invariant is untouched.

## v1.26.2071742 (July 26, 2026)

### Added

- **Quick Start panel at the top of the settings**, mirroring EPG-Janitor's
  `_section_quickstart`. It names the four actions in the order a new user
  should run them, and pre-empts the 30-day "not trustworthy" banner, which is
  the single thing most likely to be mistaken for a fault on a fresh install.
  The existing "Metricsarr is read-only" box stays directly beneath it.
  Written as one flowing paragraph rather than a multi-line list: an `info`
  body is not a safe place to rely on line breaks, and the sibling action
  `message` toast is known to collapse them.
  Unlike EPG-Janitor there is no Preview/Apply pairing to describe, because no
  action here writes to Dispatcharr.
  A test binds the copy to the `ACTIONS` list rather than to a hardcoded
  string, so renaming a button without updating the orientation copy fails the
  build instead of quietly leaving the panel pointing at a button that no
  longer exists.

### Fixed

- **The AST mutation guard's function-parameter blind spot.** The guard proves
  the plugin cannot mutate Dispatcharr by refusing any write-shaped ORM call
  whose receiver is not provably safe, but its provenance pass walked
  assignments, for-targets, `with` items, walrus and returns: every binding
  shape *except* a parameter, which is bound by the caller. So
  `def disable(ch): ch.channelprofilemembership_set.update(...)` passed
  cleanly. That is exactly the shape the Phase 2 decay ladder will reuse, so
  the guard would have been blind to the first real mutation it exists to
  police. (`.save()` and `.delete()` were always caught, being unambiguous and
  receiver-agnostic respectively; the gap was precisely the ambiguous set of
  update/add/remove/create/clear/set.)
  Closed from two directions because neither suffices alone: `<model>_set`
  reverse-related-manager **attributes** are structural proof needing no
  provenance, so they survive a cross-module call this per-file guard cannot
  trace; plus call-site parameter provenance, run to a fixed point since
  provenance now flows backwards as well as forwards. Attribute-only on
  purpose, so a plain Python set named `uuid_set` stays ordinary code.

## v1.26.2071652 (July 26, 2026)

### Changed

- **Larger type for 10-foot viewing.** The report is read from the couch on a
  TV as well as at a desk, and the new legend carries information the old badge
  pills did not. The at-a-glance chrome moves up one step: the confidence line,
  legend, caption and meter row from 13px to 15px, the status chip and hints
  from 12px to 14px, and tables from 14px to 15px. Headings and all chart
  geometry are unchanged. Confirmed on the device.

## v1.26.2071541 (July 26, 2026)

Visual polish for the generated HTML report. Presentation only — no change to
what anything means, no new per-channel field, no relaxation of the credential
allowlist. Two exceptions, both deliberate and both listed under Fixed/Changed.

### Added

- **Collapsible sections.** Each of the six report sections is now a
  `<details>`/`<summary>` — plain HTML, no JavaScript, so it still works on a TV
  browser and in a mail attachment opened with scripting off. A client that does
  not implement `<details>` renders everything expanded, so the failure mode is
  "all visible", never "content lost". Open by default: Never watched, Tuned but
  never qualified, Most used. Closed: Too new, Least used, and Excluded and
  unobservable — the last of which is ~1010 rows that used to cost a scroll on
  every load. The three closed sections carry a note that find-in-page does not
  reach inside a collapsed section on some browsers.
- **A split bar over the judged population**, with an HTML legend carrying every
  count and a caption stating the narrowing (e.g. "1440 channels · 430 judged ·
  not judged: 1010 excluded, 0 unobservable"). It is denominated on the judged
  population deliberately: over the full universe roughly 70% of the bar's ink
  went to `excluded` — the category explicitly outside judgment — while
  "tuned but never qualified", the list that matters most, rendered as an
  unlabeled sliver.
- **A sampling-density meter** with a hairline tick at the 90% coverage gate.
  Its length encodes coverage and its colour deliberately does **not** encode the
  gate verdict, which rides on a separate chip carrying a glyph and words. A
  blind-but-ticking collector produces high coverage over garbage data; a bar
  that turned green on a passing gate would render exactly that as reassuring.
  Coverage attests to sampling density, never to data validity.
- **Per-group never-watched bars** inside the rollup table, on a **judged**
  denominator rather than the group's ORM total. `total` includes excluded rows,
  so a bar drawn over it would assert a proportion the data does not support — a
  95%-excluded group would draw a short bar reading "few never-watched" when
  almost nothing in it was judged. The cell carries `data-v`, so the column sorts
  by proportion rather than looking sortable and silently doing nothing.
- Semantic colour throughout, as CSS custom properties declared for both light
  and dark. The hexes are load-bearing, not decorative: they were validated
  all-pairs for colourblind safety against this page's own surfaces. Segments
  with a zero count are dropped entirely, which is *why* all-pairs rather than
  adjacent-pairs — dropping a segment makes any two others neighbours.
  Never-watched is blue rather than the more obvious warning-orange because
  orange and red fall below the normal-vision separation floor in both modes, and
  red was worth more on "probably broken".
- A committed `tests/fixtures/sample_report.html`, regenerated by
  `scripts/render_sample.py`, so a visual check leaves an artifact a later
  session can diff instead of an unfalsifiable "I looked at it".

### Fixed

- **An action that raised rendered as success.** `run()`'s catch-all set `status`
  and `message` but no `error` — and Dispatcharr renders `error` (red,
  persistent) and `message` (a transient GREEN toast) while rendering `status`
  nowhere. This mattered before any chart existed, because `write_report` catches
  only `OSError`: anything else from `render_html` escapes to the action layer.
  The three SVG generators are correspondingly total over their inputs — NaN,
  both infinities, `None`, unparseable strings, negatives and zero denominators
  all degrade rather than raise.

### Changed

- `build_model`'s `group_rollup` buckets gain an additive **`judged`** key (the
  group's never + watched + tuned-only count). It is required by the mini bar's
  denominator and cannot be derived in the renderer, because `most_used` and
  `least_used` are top-N slices rather than the full watched set. No existing key
  changes value or meaning.

## v1.26.2070005 (July 25, 2026)

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
- **Every failure now sets `error`.** Dispatcharr's plugin card renders `.file`,
  `.error` (red, persistent) and `message` (a transient GREEN toast) -- `status`
  renders nowhere -- so metricsarr's failures, including the pre-existing bug-078
  publish guard, were pixel-identical to success.
- **`_atomic_write` uses a process-unique temp name.** A fixed `{path}.tmp` let
  two concurrent writers interleave and both `os.replace` it, publishing a torn
  report while `html_path` came back truthy.

### Added

- **"Email report now" action** (`email_report_now`). Runs the SAME three steps as
  the scheduled task -- build, verify it published, emit -- so a manual send is a
  real report, not a re-send of an old file. It deliberately does **not** write
  `last_scheduled_run_ts`: only the scheduled task may, or the button would mask a
  dead scheduler exactly as Newsflasharr's provenance-blind
  `last_attachment_delivered_ts` already does.
  Success says **"queued for delivery"**, never "sent" -- `notify()` returning True
  means durably spooled, and delivery happens later on Newsflasharr's retry ladder.
  Failure rows are ordered and all set `error`: nothing published / emit error /
  notifications off / not accepted / collector not ticking. The message also
  discloses that the honesty-gate check ran, since the button can fire a critical
  page as a side effect.
  **It does NOT prove the SCHEDULE works** -- it runs in the web worker and reads
  the current form state, while the schedule runs on a Celery worker from stored
  settings. That is what `last_scheduled_run_ts` is for.
- `notify_report.emit_report_result` -> `(ok, reason)`. `emit_report` keeps its
  bool contract.
- **`last_scheduled_run_ts`** (`scheduled_run.json`), written by
  `build_report_task` ONLY and only after the publish guard. Neither Beat's
  `total_run_count` (it counts messages SENT, not executed) nor Newsflasharr's
  `last_attachment_delivered_ts` (provenance-blind -- a manual send satisfies it
  identically) can answer "did the schedule run".
  **On first deploy this reports "the scheduled report has never run" until the
  first scheduled run lands**, since there is no prior record. That is accurate
  here and self-clears.
- `validate_settings` reports schedule health (missing / disabled / wrong queue /
  never ran) and, when `notify_enabled` is on, a Newsflasharr collector that has
  stopped ticking -- `notify()` CREATES the spool directory it writes into, so it
  returns True even when nothing will ever collect the event.
- ACTION contract tests (metricsarr pinned fields only, and its single action
  assertion was a SUBSET check, so a dropped or renamed action passed).

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
