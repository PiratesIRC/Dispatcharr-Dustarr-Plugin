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
- Self-contained HTML report served at `/logos/metricsarr/report.html`
  (Dispatcharr's own nginx static route — no extra container, no extra port),
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
  if any shipped module contains a write-shaped Django ORM call (`.save()`,
  `.update()`, `.create()`, `.delete()`, `.bulk_create()`, `.bulk_update()`,
  `.get_or_create()`, `.update_or_create()`, once the receiver is proven to
  be a Dispatcharr model or queryset) or any subprocess/`ffprobe` call. The
  Redis-facing modules (`collector`, `sessionizer`, `storage`, `gates`,
  `webhook`) are additionally verified to import no Django, ORM, or Celery
  code at all.
