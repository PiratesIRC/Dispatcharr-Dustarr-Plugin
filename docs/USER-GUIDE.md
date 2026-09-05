# Dustarr user guide

Everything you can set, every button you can press, and what each one actually
does. If you are looking for what the plugin is for, start with the
[README](../README.md). If something is broken, go to
[troubleshooting](troubleshooting.md).

- [A first run](#a-first-run)
- [What the report contains](#what-the-report-contains)
- [Where the files are written](#where-the-files-are-written)
- [Settings reference](#settings-reference)
- [Actions reference](#actions-reference)
- [The scheduled report](#the-scheduled-report)
- [Emailing the report](#emailing-the-report)
- [Updating the plugin](#updating-the-plugin)

## A first run

1. Install the plugin and enable it from Dispatcharr's **Plugins** page. Use the
   toggle on the plugin card; that is what creates the plugin's saved settings.
2. Press **Validate settings**. It writes nothing. It checks that every setting
   parses, that the collector thread is running, that the scheduled report is
   armed, and that email could actually go out if you have turned it on.
3. Wait. Dustarr reports on what it has watched happen, so it has nothing to say
   until it has been running for a while. There is no import step and no way to
   backfill: Dispatcharr does not keep the history Dustarr would need.
4. Press **Show summary** whenever you want the headline numbers without writing
   a file.
5. Press **Build report** to write the HTML report and the CSV export.

**Your first month of reports will carry a red "not trustworthy" banner.** That
is the age gate doing its job, not a fault. The unused threshold defaults to 30
days, and a dataset younger than the threshold cannot honestly call anything
unused. See [what the banner means](#the-honesty-banner).

## What the report contains

The page opens as an index. Seven sections, each collapsed, each showing its own
count and a one line description of what it holds and what to do about it. Click
a heading to open it.

![The Dustarr report as it opens: the logo beside the title, the tracking window and coverage on one line, a Report number chip, a four segment bar splitting the judged channels into never watched, watched, too new and tuned but never qualified, a sampling density meter with a sampling OK verdict beside it, and seven collapsed section headings each carrying a count](images/report-index.png)

Click a heading and its tables appear.

![The same report with the Never watched section expanded, showing a per group rollup table with a share bar, and below it a sortable table of seven channels with their group, watch count, hours, average minutes, tune count, age in days, days since last watch, last watched date and reason](images/report-expanded.png)

*Sample data, shown in dark mode. The page follows whichever theme your reader
uses. Click any column heading to sort by it, and again to reverse it. Numeric
columns compare as numbers, so 9 sorts before 10 rather than after 100.*

### The seven sections

| Section | What it holds | What to do about it |
|---|---|---|
| **Never watched** | Not watched once in the tracked window. | The dead weight. This is the list the plugin exists to produce. |
| **Channels going cold** | Watched at some point, but not once inside the cold window. | Weaker candidates than never watched, because these earned a real watch before. Entries that are still being tuned are listed apart, because that shape means broken rather than unwanted. |
| **Too new to judge** | Created more recently than the unused threshold. | Nothing. Not dead weight, just not enough time has passed. |
| **Tuned but never qualified** | You tried to watch these and gave up inside the minimum watch length. | Treat as bug reports. A dead source, a black screen, or a provider connection being dropped. Fix the source rather than turning the channel off. |
| **Least used** | The judged channels you watched least. | Retirement candidates below the never watched list. |
| **Most used** | The judged channels you watched most. | Keep these. |
| **Excluded and unobservable** | Held back from judgment by your exclusion settings, or invisible to the collector. | Review if the count surprises you. |

**Most used and least used are drawn from the judged channels only.** A watched
channel sitting in an excluded group never reaches either list. That is correct
for the question "what can I turn off" and wrong as a viewing summary, so both
sections say so on the page when it applies.

### The charts

Three, all drawn as inline SVG. Nothing is fetched from the internet, so the
report renders identically offline, on a television, or as an email attachment.

- **The bar across the top** splits the channels the plugin is willing to judge
  into never watched, watched, too new, and tuned but never qualified. Excluded
  channels are left out on purpose, because they would otherwise dominate it,
  and the caption underneath says how many were set aside. Every number in the
  bar is repeated in the legend, so no value has to be read off a colour.
- **The meter** shows how densely the collector sampled, with a tick at the 90
  percent mark it has to clear. The verdict on whether the data can be trusted
  is the separate chip beside it, never the meter colour. A collector can tick
  along perfectly while seeing nothing, and a green bar would make that look
  reassuring.
- **The small bars in the group table** show what share of each group's judged
  channels have never been watched. Judged, not total, so a group that is mostly
  excluded does not draw a misleadingly short bar.

### The report number

The chip under the title reads **Report #N**: the running total of reports this
installation has successfully written. It counts reports that reached the disk,
not times a button was pressed, so a build that failed to write does not
increment it. The same number is in `/data/dustarr/report_count.json` for
another tool to read. If it is missing or unreadable the chip does not render at
all, because a counter must never invent activity.

### The honesty banner

A red banner at the top of the report means Dustarr does not trust its own
numbers and you should not act on them. It lists exactly what looked wrong. The
usual causes:

- **The dataset is younger than the unused threshold.** Expected on a fresh
  install. Wait, or lower the threshold.
- **Sampling coverage is below 90 percent.** The collector was blind for part of
  the window, so a watch could have opened and closed unseen.
- **Implausibly few distinct channels were ever watched.** More likely a blind
  sensor than an idle household.

The banner exists because the failure it guards against is silent. A Redis
flush, a Dispatcharr upgrade that reshapes the keyspace, or a wedged collector
thread all produce a perfectly formatted report claiming every channel is dead.

## Where the files are written

| Path | What |
|---|---|
| `/config/dustarr/report.html` | The latest report. Self contained: no external stylesheet, script, font or image. |
| `/config/dustarr/report-<timestamp>.html` | The same report kept as a dated archive. The newest eight are kept whatever their age, and older ones are also deleted if you set a retention in days. |
| `/config/dustarr/report-<timestamp>.csv` | The same data as CSV, for a spreadsheet, with a commented preamble above the rows saying what the file is and what the run found. The newest eight are kept whatever their age, and older ones are also deleted if you set a retention in days. |
| `/data/dustarr/usage.json` | The recorded usage. This is the irreplaceable file: delete it and the tracking window restarts from zero. |
| `/data/dustarr/report_count.json` | The running total of reports written. |
| `/data/dustarr/scheduled_run.json` | When the scheduled report last actually ran. |

`/config` is Dispatcharr's existing bind mount, so `/config/dustarr/` is a real
folder on your host and you can open the report by double clicking it.
`/data` is a named volume with no host path, which is why nothing you need to
read is written there.

**Nothing is served over HTTP, deliberately.** Earlier versions wrote the report
into `/data/logos/`, which Dispatcharr's own nginx serves to your entire local
network with no login. That was an unauthenticated page naming every channel
your household watches.

## Settings reference

Every setting lives on the plugin's card in Dispatcharr's **Plugins** page.

### How watching is measured

These four change how watching is recorded. Changing any of them restarts the
collector, which discards any watch session in progress at that moment.

| Setting | Default | What it does |
|---|---|---|
| **Poll interval (s)** | 15 | How often the collector samples Redis. Accepts 5 to 25. It has to stay below Dispatcharr's 30 second live channel metadata timeout, or a fast channel change can happen entirely between two samples and never be seen. |
| **Minimum watch (s)** | 120 | The line between a recorded watch and a channel surf. A session shorter than this is still recorded, as a tune, which is what feeds the "tuned but never qualified" list. |
| **Client gap grace (s)** | 90 | How long a player may report zero clients, during a reconnect or a retry, before the session is treated as finished. Real retry gaps exceed 40 seconds, so a shorter value splits one real watch into several. |
| **Session merge gap (s)** | 120 | A channel tuned again within this window continues the same watch rather than starting a new one. |

### When a channel counts as unused

These change how the report reads the recorded data. Changing them does not
restart the collector and does not affect anything already recorded.

| Setting | Default | What it does |
|---|---|---|
| **Unused threshold (days)** | 30 | How old a channel has to be before it can fairly be called unused. Channels younger than this are listed as too new to judge instead. |
| **Cold threshold (days)** | 30 | How long an absence counts as cold, for the "channels going cold" list. The minimum is 7 rather than 1, because a watch is recorded only when the session ends: a channel that has been streaming continuously for longer than the window has no recent watch on record while it is still on screen. |
| **Never-watched alarm ceiling** | 0.98 | The share of judged channels that must look never watched before the data itself is treated as untrustworthy. Judged means never watched plus too new plus tuned but never qualified plus watched; excluded and unobservable channels are not counted. It is deliberately high, because a normal household shows 80 to 90 percent never watched among the channels it was ever asked to judge. It only fires on the shape where essentially every judged channel looks dead. |

### Channels that are never judged

Some channels look unused but are not, so they are kept out of the never watched
judgment. They still appear in the report, in the excluded section.

| Setting | Default | What it does |
|---|---|---|
| **Exclude auto-created channels** | on | Protects pay per view and live event slots, which sit idle between events, and which an M3U sync renames in place, so a slot that idled on "NO EVENT" for a month would otherwise read as permanently dead. |
| **Excluded groups (comma separated)** | `US: PPV, US: STL, US: News, US: NBC, US: ABC, US: CBS, US: FOX, US: Sports` | Comma separated group names that are never judged unused. Local and over the air news is the emergency broadcast tier and is supposed to sit unused. Sports has a legitimate off season. |
| **Excluded name regex** | `(?i)(LIVE EVENT|PPV|NO EVENT|24/7)` | A regular expression matched against the channel name. Anything matching is never judged unused. |

A channel whose stream profile is not proxying, for example one set to Redirect,
never writes the Redis keys the collector reads, so Dustarr cannot see it at
all. Those are reported separately as unobservable rather than being counted as
never watched. This is decided from the profile's structure, not from its name.

### The report, and what happens to it

| Setting | Default | What it does |
|---|---|---|
| **Rows in the Most used and Least used tables** | 20 | How many entries each of the two ranking lists carries. It changes the report only, never what is collected or how a channel is judged. |
| **Send notifications to Newsflasharr** | off | Hands the report summary and any honesty gate alerts to the Newsflasharr plugin. See [emailing the report](#emailing-the-report). |
| **Scheduled report** | Weekly (Mon 03:00) | Off, daily at 03:00, weekly on Monday at 03:00, or monthly on the first at 03:00. Times are in Dispatcharr's own system timezone, not UTC. |
| **Delete saved reports older than (days)** | 0 | Housekeeping for the dated copies in the config folder. After each report is built, this plugin's own `report-<stamp>.html` and `report-<stamp>.csv` files older than this many days are deleted. 0 is off, so nothing is removed unless you ask for it. The report just written is never deleted, at least one file of each kind always survives, and the live `report.html` is never touched. This sits on top of an existing cap that keeps only the newest eight of each kind whatever their age, so the two together bound the folder both ways. |

## Actions reference

The four buttons are in the order you want to press them.

### Validate settings

Writes nothing. It checks that every setting parses, reports on the collector
thread, reports on the scheduled report, and checks whether email could actually
go out.

**It cannot tell you the schedule was missing a moment ago.** Pressing any
button re-arms the schedule before the action runs, so the row it inspects was
just re-created. What it reports instead is the age of the last real scheduled
run, which is the signal that catches a schedule that has silently stopped
firing. A run stamp older than twice the schedule cadence is a red error. No
stamp at all is an informational note, because a fresh install legitimately has
none until the first cadence passes.

### Show summary

Prints the tracking window, the sampling coverage and the never watched count.
Writes nothing.

### Build report

Writes the HTML report and the CSV export immediately, and emails the report
with the file attached if notifications are on. It is the same job the schedule
runs, so the numbers are fresh rather than a re-send of an older file.

**The report is written first, before anything is emailed**, so pressing this
never wastes a run: a mail problem is reported next to a file you can still
open. With notifications off it writes the files and says so, and that is a
success rather than an error, because you asked for a file and you got one.

**It does not prove the schedule works.** This button runs in Dispatcharr's web
worker using the settings currently on screen. The schedule runs on a background
Celery worker from the saved settings. Validate settings is what answers the
schedule question.

### Report an issue

Prints the address of the plugin's issue tracker. A plugin action cannot open a
browser tab, so it prints the link for you to copy.

## The scheduled report

Off, daily, weekly or monthly, run by Celery Beat inside Dispatcharr. Weekly on
Monday at 03:00 is the default.

The only reliable evidence that a scheduled run happened is the timestamp in
`/data/dustarr/scheduled_run.json`, which is written by the scheduled task
itself after the report is confirmed published. Celery's own run counter counts
messages sent, not messages executed, so it advances whether or not the task
ever ran. Validate settings reads the timestamp for you.

**Restart the Dispatcharr container after upgrading the plugin.** Dispatcharr's
web workers reload a plugin when its `plugin.json` changes, but the Celery
workers that run the scheduled task import plugins once, at worker start, so an
in place upgrade leaves the old code running the schedule until a restart.

## Emailing the report

Dustarr does not send mail itself and does not talk to Discord or any webhook
directly. It hands its report to the
[Newsflasharr](https://github.com/PiratesIRC/Dispatcharr-Newsflasharr-Plugin) plugin,
which delivers it.

Turning on **Send notifications to Newsflasharr** is the whole setup on this
side. Where the report goes, whether that is email, Discord, ntfy or a generic
webhook, and anything to do with quiet hours or severity routing, lives in
Newsflasharr's own routing rules, keyed on the source name `dustarr`.

For the report to arrive as an email you need Newsflasharr installed and
enabled, its SMTP configured, and a routing rule sending `dustarr` to `smtp`.
Build report checks all of that and names anything missing.

**The routing rule is the check worth understanding.** Without a matching rule
the event still spools successfully and Newsflasharr still reports success; the
mail is simply delivered somewhere other than your inbox. That failure is
indistinguishable from working, which is why the button checks for the rule
specifically.

Success is reported as **queued for delivery**, not sent. Handing the report to
Newsflasharr means it was durably stored; Newsflasharr delivers it afterwards on
its own retry schedule.

If Newsflasharr is not installed, turning the setting on is harmless. Dustarr
does not spool anything and does not fail.

## Updating the plugin

1. Install the new version. Three routes, all equivalent: update it from the
   **Plugin Hub** in Dispatcharr's plugin browser, upload the release zip with
   **Import Plugin** on the **Plugins** page, or copy the `dustarr/` folder
   into Dispatcharr's plugin directory. The Hub re-hosts the release archive
   byte for byte, so no route gives you different code.
2. **Restart the Dispatcharr container.** See
   [the scheduled report](#the-scheduled-report) for why a restart is needed
   rather than optional.
3. Press **Validate settings** and check that the collector is running and the
   schedule is armed.

Your recorded usage is in `/data/dustarr/usage.json` and is not touched by an
upgrade. It is the one file worth backing up: deleting it restarts the tracking
window, and there is no way to rebuild it.
