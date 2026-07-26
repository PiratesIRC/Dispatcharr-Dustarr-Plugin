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
| `http://<your-dispatcharr-host>:9191/logos/metricsarr/report.html` | The report. Self-contained, sortable HTML with collapsible sections and inline charts — one click from any browser, including a phone or a Shield/Fire TV. Served on the **same host and port as the Dispatcharr UI you already have open** (Dispatcharr's own nginx, the `/logos/` static route) — no extra container, no extra port to open, no new address to remember. If you can reach Dispatcharr, you can reach the report. |
| `/config/metricsarr/report-<timestamp>.csv` | The same data as CSV. `/config` is Dispatcharr's existing bind mount, so this file lands somewhere on your host you can double-click straight into Excel or LibreOffice. |
| Newsflasharr (optional) | A short message with the headline numbers and a link to the full report, sent on whatever schedule you configure. See "Notifications via Newsflasharr" below. |

## Reading the report

The page opens on the three sections you can act on — **Never watched**, **Tuned
but never qualified**, and **Most used**. **Too new to judge**, **Least used**
and **Excluded and unobservable** start collapsed; click a heading to open one.
(That last section is usually the biggest by far, and it is the one you least
often need.) Collapsing is plain HTML, so it works with JavaScript off — and if
a browser doesn't support it, everything simply renders expanded. One caveat
worth knowing: on some browsers find-in-page won't reach text inside a
*collapsed* section, so expand a section before searching it.

Three charts, all drawn inline — nothing is fetched from the internet, so the
report renders the same offline, on a TV, or as an email attachment:

- **The bar across the top** splits the channels the plugin is willing to judge
  into never watched / watched / too new / tuned-but-never-qualified. It
  deliberately leaves out the excluded channels, because they'd otherwise swamp
  it — the caption underneath tells you how many were set aside and why. Every
  number in the bar is repeated in the legend below it, so you never have to
  squint at a colour to read a value.
- **The meter** shows how densely the collector actually sampled, with a tick at
  the 90% mark it needs to clear. The verdict on whether the data can be trusted
  is the separate chip beside it — **not** the meter's colour. That separation is
  intentional: a collector can tick along perfectly while seeing nothing, and a
  green bar would make that look reassuring.
- **The small bars in the group table** show what share of each group's *judged*
  channels have never been watched — judged, not total, so a group that's mostly
  excluded doesn't draw a misleadingly short bar. Click that column header to
  sort by share.

Every column in every table sorts — click a header once for ascending, again for
descending. Colour is used only to mean something, never for decoration, and the
page follows your system's light or dark theme.

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

- **It never writes to Dispatcharr's database.** This isn't just a promise in
  the README — `tests/test_no_mutations.py` reads the AST of every shipped
  module on every CI run and fails the build on any write-shaped Django ORM
  call it can prove: `.save()`, `.bulk_create()`, `.bulk_update()`,
  `.get_or_create()`, `.update_or_create()`, and the async ORM equivalents are
  flagged unconditionally; `.update()`/`.create()`/`.add()`/`.remove()`/
  `.set()`/`.clear()` are flagged once the receiver is proven to be a
  Dispatcharr model or queryset — including through a local alias, a
  `self.attr`, a for-loop variable, or a helper function's return value, not
  just a literal `Channel.objects...` at the call site; and `.delete()` is
  flagged **by default, on any receiver**, with a single narrow exception for
  the plugin's own Redis client. The same test also fails the build on
  `subprocess`/`os.system`/`os.popen`/`os.exec*`/`os.spawn*` and a bare
  `ffprobe(...)` call — no provider I/O, ever. What it does *not* and cannot
  prove: it can't see through reflection (`getattr(channel, "delete")()`),
  `eval`/`exec`, a queryset arriving as a **function parameter** (rather than
  being built or assigned in the same module the guard can see), or a write
  issued through a driver/library this guard doesn't know to look for. It's a
  strong, continuously-enforced structural guarantee against the natural ways
  an author would reach for a write, not a formal proof that no Python code
  anywhere could ever mutate the database.
- **It never contacts your provider.** No ffprobe, no stream requests of any kind —
  a single probe consumes one of your provider connections and kicks whoever is
  currently watching.
- **Credentials are redacted.** Provider credentials live inside stream URLs in a
  typical Dispatcharr setup, so every string that can reach a notification or a
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

## What to expect early on — this is by design, not broken

- **Your first ~30 days of reports will carry the "not trustworthy" banner.**
  The default "unused threshold" is 30 days, and the dataset can't be trusted
  to call anything unused until it's actually that old — a fresh install (or
  a fresh `usage.json`) is, by definition, younger than that. This is the age
  gate working as intended, not a bug. Wait it out, or lower the threshold in
  settings if you're comfortable judging on a shorter window.
- **Most of a default lineup is excluded from judgment, and that's the point.**
  On a typical box roughly 70% of channels (PPV/LIVE EVENT slots, 24/7
  channels, local/OTA news, sports) are excluded by default — see "Excluded by
  default" above for why. The actionable "turn this off" answer is drawn from
  the remaining ~30%, not the whole lineup; the never-watched ceiling gate
  (see "Key settings") is deliberately rebased on that judged remainder rather
  than the full channel count, so a healthy household can show a large
  never-watched share among it without tripping a false alarm.

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
- **Never-watched alarm ceiling** (default 0.98) — the fraction of *judged*
  channels (never-watched + too-new + tuned-but-never-qualified + watched —
  excluded/unobservable channels don't count) that must look never-watched
  before the data itself is flagged as untrustworthy. It's deliberately high:
  a normal household can show 80–90% never-watched among the channels it was
  ever asked to judge, so this only fires on the mass-casualty shape where
  essentially *every* judged channel looks dead.
- **Send notifications to Newsflasharr** — off by default. See "Notifications via
  Newsflasharr" below.
- **Report base URL** — the base URL of your Dispatcharr UI (e.g.
  `http://192.168.1.53:9191`). Set this so the report link sent to Newsflasharr
  is an actual clickable link — without it, some notification channels render a
  bare path as inert text.
- **Scheduled report** — off / daily / weekly / monthly, run by Celery Beat.

## Notifications via Newsflasharr

Metricsarr no longer talks to Discord (or any webhook) directly. Instead it has
one setting, **Send notifications to Newsflasharr** (off by default), that hands
its report summary and any honesty-gate alerts to the
[Newsflasharr](https://github.com/PiratesIRC/Dispatcharr_Newsflasharr) plugin if
it's installed and enabled.

Turning the toggle on is the whole caller-side setup. Everything else —
**which channel(s) it goes to (Discord, ntfy, email, a generic webhook, ...),
whether it's routed differently by severity, quiet hours, storm dedup** — lives
entirely in Newsflasharr's own routing rules, keyed on the source name
`metricsarr`. See the Newsflasharr plugin's own docs for how to add a routing
rule and pick a destination channel; nothing on Metricsarr's side needs to
change to redirect where its notifications land.

If Newsflasharr isn't installed or isn't enabled, turning this setting on is
harmless — Metricsarr degrades safely and simply doesn't spool anything.

### Email report now

**Email report now** builds the report immediately and emails it with the file
attached — the same job the schedule runs, so you get fresh data rather than a
re-send of an older file.

The wording it reports back is deliberate:

- Success says **"queued for delivery"**, not "sent". Handing the report to
  Newsflasharr means it was durably spooled; Newsflasharr delivers it afterwards
  on its own retry schedule.
- If notifications are off, nothing was published, Newsflasharr declined the
  event, or its collector has stopped running, you get a **red error** naming
  which — never a green tick.

**It does not prove the *schedule* works.** The button runs in the web worker
using the settings currently on screen; the schedule runs on a background worker
from saved settings. **Validate settings** is what tells you about the schedule:
it reports when the scheduled report last ran, and warns if it has never run, is
disabled, or is queued to a worker that would reject it.

## Install / upgrade

Copy the `metricsarr/` folder into Dispatcharr's plugin directory, or install the
release zip from the plugin UI. **Restart the Dispatcharr container after
upgrading** — Dispatcharr's web workers hot-reload a plugin when `plugin.json`'s
modified time changes, but the Celery workers that run the scheduled report task
only import plugins once, at worker start, so an in-place upgrade leaves the old
code running in Celery until the container restarts.
