# Metricsarr

A Dispatcharr plugin that records which channels are actually watched, and reports
the most-used, least-used, and never-watched channels so you can turn off the dead
weight in your lineup.

**Phase 1 is read-only. It never changes a channel, a stream, or anything else in
Dispatcharr's database.** It only reads Redis (to see who's watching what) and the
channel list (to know what exists), and writes its own report files.

## What it does

A leader-elected collector polls Dispatcharr's live-proxy Redis state every 15
seconds (configurable) and turns the raw client counts into watch sessions. A
session only counts as a **watch** once it has run for at least `min_watch_seconds`
(120s by default) — flipping through channels while looking for something to watch
doesn't inflate the numbers. A short session that never reaches that threshold still
gets recorded as a **tune**, which matters for the third list below.

Exactly one collector runs at a time (a Redis lease elects the leader across worker
processes), it degrades safely if Redis or the plugin's own storage hiccups, and it
never blocks Dispatcharr's web UI or the Celery workers — reporting happens on a
separate scheduled task, not inline with the collector.

## Where the reports land

| Where | What |
|---|---|
| `http://<your-dispatcharr-host>:9191/logos/metricsarr/report.html` | The report. Self-contained, sortable HTML — one click from any browser, including a phone or a Shield/Fire TV. Served by Dispatcharr's own nginx (the `/logos/` static route), so there's no extra container and no extra port to open. |
| `/config/metricsarr/report-<timestamp>.csv` | The same data as CSV. `/config` is Dispatcharr's existing bind mount, so this file lands somewhere on your host you can double-click straight into Excel or LibreOffice. |
| A webhook (Discord or generic JSON) | A short message with the headline numbers and a link to the full report, sent on whatever schedule you configure. |

## The three lists that matter

- **Never watched** — the dead weight. Entries are grouped by channel group so you
  can act on an entire group at once instead of clicking through channels one at a
  time.
- **Tuned but never qualified** — channels you *tried* to watch and gave up on
  within two minutes. This list is not "barely used"; it is almost certainly
  **broken**: a dead source, a black-screen slate, or a provider connection getting
  kicked mid-stream. Treat entries here as bug reports and go fix the source, don't
  disable the channel.
- **Most / least used** — the ordinary leaderboard, by watch count and hours.

Two more sections round out the report: **too new to judge** (channels created too
recently to fairly call unused — not dead weight, just not enough time has passed
yet) and **excluded / unobservable** (see below).

## Excluded by default

Some channels look unused but aren't, so Metricsarr keeps them out of the
never-watched judgment by default (all of this is configurable in the plugin
settings):

- **Auto-created channels** — PPV `LIVE EVENT` slots sit idle between fights and
  events, and the provider's M3U sync renames the same channel row in place
  (so a slot that idled on "NO EVENT" for a month would otherwise read as
  permanently dead).
- **Local/OTA news** — the emergency-broadcast tier. It's supposed to sit unused
  most of the time.
- **Sports** — has a legitimate off-season, so a quiet month doesn't mean unused.

A channel whose stream profile isn't proxying (e.g. set to Redirect) never writes
the Redis keys the collector polls, so it's structurally invisible to Metricsarr —
it's reported separately as **unobservable**, not folded into never-watched.

## Safety

- **It never writes to Dispatcharr's database.** This isn't a promise in the
  README — `tests/test_no_mutations.py` reads the AST of every shipped module on
  every CI run and fails the build if a write-shaped Django ORM call (`.save()`,
  `.update()`, `.create()`, `.delete()`, `.bulk_create()`, and friends, once the
  receiver is proven to be a Dispatcharr model or queryset) exists anywhere in the
  plugin. The same test also fails the build on any subprocess or `ffprobe` call.
- **It never contacts your provider.** No ffprobe, no stream requests of any kind —
  a single probe consumes one of your provider connections and kicks whoever is
  currently watching.
- **Credentials are redacted.** Provider credentials live inside stream URLs in a
  typical Dispatcharr setup, so every string that can reach a webhook payload or a
  logged error is passed through a redactor first, and the report only ever renders
  an allowlisted set of fields (channel name, group, counts, timestamps) — never a
  stream URL.
- **If the collector goes blind, the report says so loudly.** A Redis flush, a
  Dispatcharr upgrade that reshapes the keyspace, or a wedged collector thread would
  otherwise quietly produce a report claiming the household watches nothing.
  Metricsarr checks sampling coverage and watch plausibility before trusting its own
  data, and if those checks fail it puts a loud banner at the top of the report
  listing exactly what looked wrong, instead of silently telling you every channel
  is dead.

## Key settings

Everything lives in the plugin's settings card in the Dispatcharr UI:

- **Poll interval** (default 15s) — how often the collector samples Redis. Must
  stay under Dispatcharr's 30-second live-channel metadata TTL, or a fast channel
  switch can be missed between polls.
- **Minimum watch** (default 120s) — the line between a recorded watch and a
  channel-surf.
- **Client gap grace** (default 90s) — how long a player may show zero clients (a
  reconnect/retry) before the watch session is considered over, so one real watch
  with a brief player hiccup isn't recorded as two or three separate ones.
- **Excluded groups / excluded name pattern / exclude auto-created** — tune what's
  kept out of the never-watched judgment (see "Excluded by default" above).
- **Unused threshold (days)** — how old a channel must be before it can fairly be
  called unused.
- **Webhook URL / format** — Discord or generic JSON.
- **Scheduled report** — off / daily / weekly / monthly, run by Celery Beat.

## Install / upgrade

Copy the `metricsarr/` folder into Dispatcharr's plugin directory, or install the
release zip from the plugin UI. **Restart the Dispatcharr container after
upgrading** — Dispatcharr's web workers hot-reload a plugin when `plugin.json`'s
modified time changes, but the Celery workers that run the scheduled report task
only import plugins once, at worker start, so an in-place upgrade leaves the old
code running in Celery until the container restarts.
