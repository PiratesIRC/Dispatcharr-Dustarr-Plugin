# Changelog

## Unreleased

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
