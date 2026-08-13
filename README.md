# Dustarr

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/PiratesIRC)

A Dispatcharr plugin that records which channels are actually watched, and reports
the most-used, least-used, and never-watched channels so you can turn off the dead
weight in your lineup.

**Phase 1 is read-only. It never changes a channel, a stream, or anything else in
Dispatcharr's database.** It only reads Redis (to see who is watching what) and the
channel list (to know what exists), and writes its own report files.

## What it does

A leader-elected collector polls Dispatcharr's live-proxy Redis state every 15
seconds (configurable) and turns the raw client counts into watch sessions. A
session only counts as a **watch** once it has run for at least `min_watch_seconds`
(120s by default), so flipping through channels while looking for something to watch
does not inflate the numbers. A short session that never reaches that threshold still
gets recorded as a **tune**, which matters for the third list below.

Exactly one collector runs at a time (a Redis lease elects the leader across worker
processes), it degrades safely if Redis or the plugin's own storage hiccups, and it
never blocks Dispatcharr's web UI or the Celery workers. Reporting happens on a
separate scheduled task, not inline with the collector.

## Where the reports land

| Where | What |
|---|---|
| `/config/dustarr/report.html` | The report, always the latest run. Self-contained, sortable HTML with collapsible sections and inline charts. `/config` is Dispatcharr's existing bind mount, so this is a real folder on your host: open the file by double-clicking it, or copy it anywhere you like. |
| `/config/dustarr/report-<timestamp>.html` | The same report, kept as a dated archive. The last eight are retained. |
| `/config/dustarr/report-<timestamp>.csv` | The same data as CSV, for a spreadsheet. The last eight are retained. |
| Newsflasharr (optional) | A short message with the headline numbers, with the full HTML report attached, sent on whatever schedule you configure. See "Notifications via Newsflasharr" below. |
| `/data/dustarr/report_count.json` | A running total of reports that have been successfully written, as `{"reports_built": N}`. It is there for another tool to read; you never need to look at it. See "Counting reports" below. |

**Nothing is served over HTTP, deliberately.** Earlier versions wrote the report
into `/data/logos/`, which Dispatcharr's own nginx serves to your whole local
network with no login. That was convenient and it was also an unauthenticated,
unlisted-but-guessable page naming every channel your household watches. The
report is now a file on a bind mount you already have, and it reaches you by
email as an attachment if you turn notifications on.

## Reading the report

Every section starts collapsed, so the page opens as an index rather than a wall
of tables: six headings, each with its count and a one-line description of what
it holds and what to do about it. Click a heading to open one. Collapsing is
plain HTML, so it works with JavaScript off, and a browser that does not support
it simply renders everything expanded. One caveat worth knowing: on some
browsers find-in-page will not reach text inside a *collapsed* section, so
expand a section before searching it. Every section says so itself.

Three charts, all drawn inline. Nothing is fetched from the internet, so the
report renders the same offline, on a TV, or as an email attachment:

- **The bar across the top** splits the channels the plugin is willing to judge
  into never watched / watched / too new / tuned-but-never-qualified. It
  deliberately leaves out the excluded channels, because they would otherwise
  swamp it, and the caption underneath tells you how many were set aside and why.
  Every number in the bar is repeated in the legend below it, so you never have to
  squint at a colour to read a value.
- **The meter** shows how densely the collector actually sampled, with a tick at
  the 90% mark it needs to clear. The verdict on whether the data can be trusted
  is the separate chip beside it, **not** the meter's colour. That separation is
  intentional: a collector can tick along perfectly while seeing nothing, and a
  green bar would make that look reassuring.
- **The small bars in the group table** show what share of each group's *judged*
  channels have never been watched. Judged, not total, so a group that is mostly
  excluded does not draw a misleadingly short bar. Click that column header to
  sort by share.

Two columns answer the recency question directly: **Days since**, the number of
days since a channel was last watched, and **Avg min**, the average length of a
watch on it. Sorting by "Days since" turns the table into an ordered retirement
queue. Channels never watched read "never" in that column and still sort as the
coldest, and both columns appear in the CSV export too.

Every column in every table sorts: click a header once for ascending, again for
descending. Colour is used only to mean something, never for decoration, and the
page follows your system's light or dark theme.

## The lists that matter

- **Never watched**: the dead weight. Entries are grouped by channel group so you
  can act on an entire group at once instead of clicking through channels one at a
  time.
- **Channels going cold**: watched at some point, but not once inside the cold
  window. These earned a real watch before, so they are weaker candidates to turn
  off than the never-watched list and stronger than anything below it. Two things
  are deliberately kept out of the abandoned list: a channel the collector cannot
  observe is never judged by its silence, and a channel tuned again recently but
  given up on before the minimum watch length is listed separately as still being
  tried, because that shape means broken rather than unwanted. If the tracked
  dataset is younger than seven days the section says it cannot answer yet instead
  of listing anything.
- **Tuned but never qualified**: channels you *tried* to watch and gave up on
  within two minutes. This list is not "barely used"; it is almost certainly
  **broken**: a dead source, a black-screen slate, or a provider connection getting
  kicked mid-stream. Treat entries here as bug reports and go fix the source rather
  than disabling the channel.
- **Most / least used**: the ordinary leaderboard, by watch count and hours.

Two more sections round out the report: **too new to judge** (channels created too
recently to fairly call unused, so not dead weight, just not enough time has passed
yet) and **excluded / unobservable** (see below).

## Excluded by default

Some channels look unused but are not, so Dustarr keeps them out of the
never-watched judgment by default (all of this is configurable in the plugin
settings):

- **Auto-created channels**: PPV `LIVE EVENT` slots sit idle between fights and
  events, and the provider's M3U sync renames the same channel row in place
  (so a slot that idled on "NO EVENT" for a month would otherwise read as
  permanently dead).
- **Local/OTA news**: the emergency-broadcast tier. It is supposed to sit unused
  most of the time.
- **Sports**: has a legitimate off-season, so a quiet month does not mean unused.

A channel whose stream profile is not proxying (e.g. set to Redirect) never writes
the Redis keys the collector polls, so it is structurally invisible to Dustarr. It
is reported separately as **unobservable**, not folded into never-watched.

## Safety

- **It never writes to Dispatcharr's database.** This is not just a promise in
  the README. `tests/test_no_mutations.py` reads the AST of every shipped
  module on every CI run and fails the build on any write-shaped Django ORM
  call it can prove: `.save()`, `.bulk_create()`, `.bulk_update()`,
  `.get_or_create()`, `.update_or_create()`, and the async ORM equivalents are
  flagged unconditionally; `.update()`/`.create()`/`.add()`/`.remove()`/
  `.set()`/`.clear()` are flagged once the receiver is proven to be a
  Dispatcharr model or queryset, including through a local alias, a
  `self.attr`, a for-loop variable, or a helper function's return value, not
  just a literal `Channel.objects...` at the call site; and `.delete()` is
  flagged **by default, on any receiver**, with a single narrow exception for
  the plugin's own Redis client. The same test also fails the build on
  `subprocess`/`os.system`/`os.popen`/`os.exec*`/`os.spawn*` and a bare
  `ffprobe(...)` call, so there is no provider I/O, ever. What it does *not* and
  cannot prove: it cannot see through reflection (`getattr(channel, "delete")()`),
  `eval`/`exec`, a queryset arriving as a **function parameter** (rather than
  being built or assigned in the same module the guard can see), or a write
  issued through a driver or library this guard does not know to look for. It is a
  strong, continuously-enforced structural guarantee against the natural ways
  an author would reach for a write, not a formal proof that no Python code
  anywhere could ever mutate the database.
- **It never contacts your provider.** No ffprobe, and no stream requests of any
  kind. A single probe consumes one of your provider connections and kicks whoever
  is currently watching.
- **Credentials are redacted.** Provider credentials live inside stream URLs in a
  typical Dispatcharr setup, so every string that can reach a notification or a
  logged error is passed through a redactor first, and the report only ever renders
  an allowlisted set of fields (channel name, group, counts, timestamps), never a
  stream URL.
- **If the collector goes blind, the report says so loudly.** A Redis flush, a
  Dispatcharr upgrade that reshapes the keyspace, or a wedged collector thread would
  otherwise quietly produce a report claiming the household watches nothing.
  Dustarr checks sampling coverage and watch plausibility before trusting its own
  data, and if those checks fail it puts a loud banner at the top of the report
  listing exactly what looked wrong, instead of silently telling you every channel
  is dead.

## What to expect early on, and why it is by design

- **Your first 30 days or so of reports will carry the "not trustworthy" banner.**
  The default "unused threshold" is 30 days, and the dataset cannot be trusted
  to call anything unused until it is actually that old. A fresh install (or
  a fresh `usage.json`) is, by definition, younger than that. This is the age
  gate working as intended, not a bug. Wait it out, or lower the threshold in
  settings if you are comfortable judging on a shorter window.
- **Most of a default lineup is excluded from judgment, and that is the point.**
  On a typical box roughly 70% of channels (PPV/LIVE EVENT slots, 24/7
  channels, local/OTA news, sports) are excluded by default. See "Excluded by
  default" above for why. The actionable "turn this off" answer is drawn from
  the remaining 30% or so, not the whole lineup; the never-watched ceiling gate
  (see "Key settings") is deliberately rebased on that judged remainder rather
  than the full channel count, so a healthy household can show a large
  never-watched share among it without tripping a false alarm.

## If you have just installed it

The plugin's settings page opens with a **Quick Start** panel, and the buttons
sit in the order you want to press them: **Validate settings** first, then
**Show summary** for the headline numbers, then **Build report** to write the
HTML and CSV, which also emails them if you have Newsflasharr notifications on.
**Report an issue** prints the link to the issue tracker.

The one thing worth knowing up front: your first reports will carry a "not
trustworthy" banner, and that is the age gate working rather than a fault. See
"What to expect early on" below.

## Key settings

Everything lives in the plugin's settings card in the Dispatcharr UI:

- **Poll interval** (default 15s): how often the collector samples Redis. Must
  stay under Dispatcharr's 30-second live-channel metadata TTL, or a fast channel
  switch can be missed between polls.
- **Minimum watch** (default 120s): the line between a recorded watch and a
  channel-surf.
- **Client gap grace** (default 90s): how long a player may show zero clients (a
  reconnect or retry) before the watch session is considered over, so one real watch
  with a brief player hiccup is not recorded as two or three separate ones.
- **Excluded groups / excluded name pattern / exclude auto-created**: tune what is
  kept out of the never-watched judgment (see "Excluded by default" above).
- **Unused threshold (days)**: how old a channel must be before it can fairly be
  called unused.
- **Cold threshold (days)** (default 30, minimum 7): how long an absence counts as
  cold, for the "Channels going cold" list. The minimum is seven days rather than
  one because a watch is recorded only when the session ends, so a channel that has
  been streaming continuously for longer than the window has no recent watch and no
  recent tune on record while it is still on screen.
- **Never-watched alarm ceiling** (default 0.98): the fraction of *judged*
  channels (never-watched + too-new + tuned-but-never-qualified + watched, since
  excluded and unobservable channels do not count) that must look never-watched
  before the data itself is flagged as untrustworthy. It is deliberately high:
  a normal household can show 80 to 90% never-watched among the channels it was
  ever asked to judge, so this only fires on the mass-casualty shape where
  essentially *every* judged channel looks dead.
- **Send notifications to Newsflasharr**: off by default. See "Notifications via
  Newsflasharr" below.
- **Scheduled report**: off / daily / weekly / monthly, run by Celery Beat.

## Counting reports

Dustarr keeps a running total of the reports it has successfully written, in
`/data/dustarr/report_count.json`:

```json
{"reports_built": 42}
```

It increments once per report whose HTML file actually reached the disk,
whether you pressed the button or the schedule ran it. A build that failed to
write does not count, which matters more than it sounds: the report writer
degrades rather than raising, so a failed publish otherwise looks the same as
a good one.

It exists so another tool can display the number, for example as a badge. The
notification email also carries a `report number N` line, but that is for a
person reading the mail. Anything counting the number should read the file:
the shared notification client has a fixed payload with no field for extra
data, so nothing structured can travel that path.

One honest limitation: the counter can undercount. Incrementing it is a
read-then-write with no lock, and Dispatcharr runs several worker processes,
so two reports finishing in the same instant can lose one increment. Locking a
file on the request path is a worse trade than an occasional miss in a
cosmetic number, and in practice reports are minutes apart.

`docs/newsflasharr-report-count-spec.md` describes what the Newsflasharr
plugin would need in order to turn this into a badge.

## Notifications via Newsflasharr

Dustarr no longer talks to Discord (or any webhook) directly. Instead it has
one setting, **Send notifications to Newsflasharr** (off by default), that hands
its report summary and any honesty-gate alerts to the
[Newsflasharr](https://github.com/PiratesIRC/Dispatcharr_Newsflasharr) plugin if
it is installed and enabled.

Turning the toggle on is the whole caller-side setup. Everything else, meaning
**which channel or channels it goes to (Discord, ntfy, email, a generic webhook,
and so on), whether it is routed differently by severity, quiet hours, storm
dedup**, lives entirely in Newsflasharr's own routing rules, keyed on the source
name `dustarr`. See the Newsflasharr plugin's own docs for how to add a routing
rule and pick a destination channel; nothing on Dustarr's side needs to
change to redirect where its notifications land.

If Newsflasharr is not installed or is not enabled, turning this setting on is
harmless: Dustarr degrades safely and simply does not spool anything.

### Build report

**Build report** writes the report immediately and, if notifications are on,
emails it with the file attached. It is the same job the schedule runs, so you
get fresh data rather than a re-send of an older file. There used to be two
buttons here, **Build report** and **Email report now**; they did the same work
and differed only in whether the email step ran, which the **Send notifications
to Newsflasharr** setting already answers.

**The report is written either way**, so pressing this never wastes a run. If
notifications are off it simply says so and stops, with no error: you asked for
a file and you got one.

If notifications are on, emailing needs Newsflasharr installed and enabled, with
its SMTP configured and a routing rule sending dustarr to smtp. The button
checks all of that and names anything missing, next to the report it just wrote.
The routing check is the one that earns its keep: without a matching rule the
event still spools successfully and is simply delivered somewhere else, which
looks exactly like working.

The wording it reports back is deliberate:

- Success says **"queued for delivery"**, not "sent". Handing the report to
  Newsflasharr means it was durably spooled; Newsflasharr delivers it afterwards
  on its own retry schedule.
- If nothing was published, or notifications are on but Newsflasharr is not
  ready, declined the event, or its collector has stopped running, you get a
  **red error** naming which one, never a green tick.

**It does not prove the *schedule* works.** The button runs in the web worker
using the settings currently on screen; the schedule runs on a background worker
from saved settings. **Validate settings** is what tells you about the schedule:
it reports when the scheduled report last ran, and warns if it has never run, is
disabled, or is queued to a worker that would reject it.

## Install / upgrade

Copy the `dustarr/` folder into Dispatcharr's plugin directory, or install the
release zip from the plugin UI. **Restart the Dispatcharr container after
upgrading.** Dispatcharr's web workers hot-reload a plugin when `plugin.json`'s
modified time changes, but the Celery workers that run the scheduled report task
only import plugins once, at worker start, so an in-place upgrade leaves the old
code running in Celery until the container restarts.

## Further reading

- [docs/troubleshooting.md](docs/troubleshooting.md) if something is not
  working: no report arrived, nothing is being recorded, the numbers look wrong,
  or the email is not turning up.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how the plugin is put together
  and the constraints that are not obvious from one file.
- [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or a pull request.
- [SECURITY.md](SECURITY.md) for what the plugin can reach, what the report
  contains, and how to report a vulnerability privately.

## Changelog

Release notes are in [docs/CHANGELOG.md](docs/CHANGELOG.md), newest first. Each
entry says what changed from the operator's point of view, not what changed in
the code.

## Versioning

Calendar versioning, `Major.YY.DDDHHMM`: major version, two-digit year,
day-of-year, then the UTC time the version was cut. `1.26.2241505` is major
version 1, built on day 224 of 2026 at 15:05 UTC. A later version string is
always a later build.

## Sponsor

This plugin is free and always will be. If it saves you time and you would like
to support the work, you can sponsor it at
[github.com/sponsors/PiratesIRC](https://github.com/sponsors/PiratesIRC).

Sponsoring buys no priority, no private support and no influence over what gets
built. Bug reports and pull requests are just as welcome from everyone.

## License

MIT. See [LICENSE](LICENSE).
