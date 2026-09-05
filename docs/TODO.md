# Open work

What is planned, what is deliberately not planned, and the measurement behind
each item. Newest thinking first within each section. Anything already shipped
is in the [changelog](CHANGELOG.md) rather than here.

## Planned

### Phase 2: the decay ladder

The plugin currently reports and stops. Phase 2 would act on what it finds, in
escalating steps: report, then rename, then move to a holding group, then
disable. **Deleting a channel is not on the ladder and will not be added.**

It is specified but not built. Its prerequisite is done: the syntax tree guard
that proves no database write exists had a blind spot where a queryset arrived
as a function parameter, and that is closed, so the guard can be trusted to
police the boundary once real writes exist on the other side of it.

Nothing here should be built on data the plugin will not vouch for. Phase 2 acts
on the numbers, so it must refuse to act while the honesty banner is showing.

### Time windowed metrics, releases B to D

Release A shipped the two recency columns, days since last watch and average
session length, plus the "channels going cold" section. The remaining releases
add per channel daily and monthly buckets so the report can show a trend rather
than a single running total.

**The trap that will bite whoever builds this**: the report's usage loader
rebuilds every record from a fixed list of keys, so any new key written into the
usage file is silently discarded before the report ever sees it. A feature added
without extending that list renders zeros forever and raises no error. The
collector's own loader deliberately behaves the opposite way and preserves keys
it does not recognise, because it rewrites the whole usage structure on every
flush and an allowlist there would delete the new data on every restart.

### An attachment size check

There is none anywhere in the plugin. The notification path passes the report
path straight through, and the shared notification client limits the message
body only, so an oversized report is refused at the far end with nothing visible
on this side.

The measured margin: a real report is about 701 kilobytes against a 1 mebibyte
attachment limit, so roughly a third spare, and the remaining time windowed
releases add columns to every table.

The check belongs in the plugin's own notification module, not in the shared
client, which is vendored byte for byte into six separate plugins behind a hash
pin. It should degrade by sending the summary without the attachment and setting
a visible error naming the size and the path on disk, because the report file is
always written before anything is emailed.

### Branch protection on the repository

Not applied. The reason it was not applied has expired: repository rulesets
require a paid plan only on a private repository, and this one has been public
since 2026-08-25. Leaving the default branch unprotected is defensible for a
single maintainer who pushes directly and runs the full test suite plus a
publish audit before every push, but it is now a choice rather than a
limitation.

### The Discord thread is missing from the plugin's own manifest

`dustarr/plugin.json` has no `discord_thread` field, while the sibling plugins
carry one and the Plugin Hub listing already has it.

This is deliberate rather than forgotten. Dispatcharr does not read that field
from a plugin manifest, measured by searching the running container, so adding
it changes nothing at runtime. Editing a file that ships in the release archive
for no functional reason would make the repository, the release and the
installed copy differ with nothing in the version to explain it, and verifying
those three against each other is how every deploy here is checked.

**This note previously said it would go in with the next release. Release
1.26.2481620 shipped on 2026-09-05 without it**, because it was not in that
release's scope and adding a field after the publish audit had run would have
changed what was audited. Carrying it now would need its own version bump. It
is still worth doing, and the honest status is that it is pending rather than
scheduled.

## Deliberately not planned

- **Deleting channels.** See the decay ladder above.
- **Anything that contacts the provider.** No stream requests and no `ffprobe`.
  A single probe consumes one of the account's connections and drops whoever is
  currently watching. The syntax tree guard fails the build on either.
- **Serving the report over HTTP.** An earlier version wrote it where
  Dispatcharr's own web server publishes files to the whole local network with
  no login, which made an unauthenticated page naming every channel a household
  watches. Three tests exist specifically to stop that being reintroduced, and
  there will not be a report URL setting.
- **Relaxing the default exclusions to make the rankings a viewing summary.**
  The exclusions protect the emergency broadcast tier and the pay per view slots
  that an M3U sync renames in place. The rankings say on the page that they omit
  excluded channels; that note is the fix, not removing the exclusions.
- **Backfilling history.** Dispatcharr does not retain the state the collector
  reads, so there is nothing to import. A fresh installation really does start at
  zero.

## Known limitations, accepted

- **The report counter can undercount.** Incrementing it is a read followed by a
  write with no lock, and Dispatcharr runs several worker processes, so two
  reports finishing in the same instant can lose one increment. Locking a file
  on the request path is a worse trade than an occasional miss in a number that
  is displayed and never acted on.
- **Sampling coverage cannot detect a collector that keeps sampling but sees
  nothing.** Coverage measures how densely the collector ticked, not whether
  what it read was correct. A collector whose view of the keyspace has gone
  empty ticks perfectly and scores full coverage forever. The plausibility gates
  are what catch that shape, which is why the report shows the coverage meter
  and the trust verdict as two separate things rather than colouring the meter.
- **Validate settings cannot report a schedule that was missing a moment ago.**
  Every button re-arms the schedule before the action runs, so the row it
  inspects was just re-created. It reports the age of the last real run instead.
  The accepted blind spot is an installation that has never had a successful
  scheduled run at all: that stays a note rather than an error, because treating
  it as an error made every fresh installation show a red alarm for a week.
- **The logo has 255 transparent pixels inside the disc, 0.389 percent of the
  image.** On a dark background they read as the intended dark dots; on a light
  background they show through. At the 48 pixel size the report renders it at,
  this is not visible. Fill them with the disc colour only if it is ever scaled
  up.

## Verification

- **The publish audit's new skip path has NOT been seen on a real pull request,
  measured 2026-09-05.** The workflow used to fail on every Dependabot pull
  request, because GitHub withholds repository secrets from those and the audit
  scripts fail closed when the deny list is absent. It now skips with a notice,
  and only when the value is empty and the event is a pull request.

  All five branches of that decision were exercised locally, and a manual run on
  the real runner confirmed the scan still executes when the secret IS present:
  7 deny and 11 allow rules compiled, 69 files scanned, no findings. What has
  not been observed is the skip itself, because no fork or Dependabot pull
  request has been open since. **The next Dependabot update is its first real
  test. Check that it reports a skip rather than a pass, and that a push to the
  default branch still fails hard when the deny list is missing.**

- **The scheduled report is confirmed running, measured 2026-08-24.** This was
  open for a month: a schedule row deleted by a plugin reload had cost two
  chances, and no scheduled run had been observed since the plugin was renamed.
  It is now settled by side effects rather than by a counter. The run timestamp
  in `/data/dustarr/scheduled_run.json` has advanced to 2026-08-24 08:00 UTC,
  and two report files sit on disk timestamped 03:00 local on 2026-08-17 and
  2026-08-24, which are the Mondays the weekly schedule names.

  Judging it by side effects is the reason it is settled. Celery's own run counter reads 2, and
  it would read 2 whether or not the task ever executed, because it counts
  messages sent. Pressing the Build report button deliberately does not write
  the timestamp, so that the one signal answering this question could not be
  destroyed by a hand run.
