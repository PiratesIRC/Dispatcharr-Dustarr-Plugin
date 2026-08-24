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

Not applied. Repository rulesets require a paid plan on a private repository and
the API refuses the request. Either the repository becomes public, or the
protection waits, which is a reasonable position for a single maintainer.

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
  reads, so there is nothing to import. A fresh installation genuinely starts at
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

## Verification still outstanding

- **A scheduled report has not yet been observed running under the current
  plugin name.** The mechanism itself is proven: the run timestamp recorded a
  real scheduled run before the plugin was renamed. Two later chances were lost
  to a schedule row that had been deleted by a plugin reload, which is fixed.
  The proof is the timestamp in `/data/dustarr/scheduled_run.json` advancing.
  Pressing the Build report button deliberately does not write that timestamp,
  because only the scheduled task may write it and a hand run would destroy the
  one signal that answers the question.
