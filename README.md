<img src="dustarr/logo.png" alt="The Dustarr logo: a dark radar scope with a green sweep line and coloured blips scattered across it" width="110" align="right">

# Dustarr

A Dispatcharr plugin that records which channels are actually watched, and reports the ones that are not, so you can turn off the dead weight in your lineup.

> [!TIP]
> **New to Dispatcharr plugins?** Start with the **[Dispatcharr Plugin Workflow guide](https://piratesirc.github.io/Dispatcharr-Plugin-Workflow/)**.
> It explains what each plugin and tool does, where they overlap, and what order to use them in.

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Workflow Guide](https://img.shields.io/badge/%F0%9F%93%96-Workflow_Guide-1F6FEB?style=flat)](https://piratesirc.github.io/Dispatcharr-Plugin-Workflow/)
[![Discord](https://img.shields.io/badge/Discord-Discussion-5865F2?logo=discord&logoColor=white)](https://discord.com/channels/1340492560220684331/1542141054080524310)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/PiratesIRC)

[![GitHub Release](https://img.shields.io/github/v/release/PiratesIRC/Dispatcharr-Dustarr-Plugin?include_prereleases&logo=github)](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/releases)
[![Downloads](https://img.shields.io/github/downloads/PiratesIRC/Dispatcharr-Dustarr-Plugin/total?color=success&label=Downloads&logo=github)](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/releases)
[![Stars](https://img.shields.io/github/stars/PiratesIRC/Dispatcharr-Dustarr-Plugin?logo=github)](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/stargazers)

![Top Language](https://img.shields.io/github/languages/top/PiratesIRC/Dispatcharr-Dustarr-Plugin)
![Repo Size](https://img.shields.io/github/repo-size/PiratesIRC/Dispatcharr-Dustarr-Plugin)
![Last Commit](https://img.shields.io/github/last-commit/PiratesIRC/Dispatcharr-Dustarr-Plugin)
![License](https://img.shields.io/github/license/PiratesIRC/Dispatcharr-Dustarr-Plugin)

**It never changes anything in Dispatcharr.** It reads which channels have viewers, and writes its own report files. Nothing else.

## Features

**Recording what is watched**
* **A leader elected collector** samples Dispatcharr's live proxy state every 15 seconds and turns raw client counts into watch sessions. Exactly one collector runs at a time across all worker processes, so nothing is double counted.
* **Watches and tunes are separate.** A session counts as a watch only once it has run for two minutes, so flipping through channels looking for something does not inflate the numbers. A shorter session is still recorded, as a tune, and that distinction is what produces the broken channel list below.
* **Player hiccups do not split a watch.** A player may drop to zero clients for up to 90 seconds, during a reconnect or a retry, without the session being treated as finished.
* **It never blocks Dispatcharr.** The collector runs on its own, and reporting is a separate scheduled job rather than something that happens while you are waiting for a page.

**The lists it produces**
* **Never watched**: the dead weight, grouped by channel group so you can act on a whole group at once.
* **Tuned but never qualified**: channels you tried to watch and gave up on within two minutes. This list is not "barely used", it is almost certainly **broken**: a dead source, a black screen, or a provider connection being dropped. Treat these as bug reports rather than turning them off.
* **Channels going cold**: watched at some point, but not lately. Entries that are still being tuned are listed apart, because that shape also means broken rather than unwanted.
* **Too new to judge** and **most / least used** round it out.

**Not judging channels that only look unused**
* **Pay per view and live event slots** sit idle between events, and an M3U sync renames the same channel row in place, so a slot that idled on "NO EVENT" for a month would otherwise read as permanently dead.
* **Local and over the air news** is the emergency broadcast tier. It is supposed to sit unused.
* **Sports** has a legitimate off season.
* **Channels the collector cannot see at all** (a stream profile set to Redirect writes none of the state the collector reads) are reported separately as unobservable rather than being counted as never watched. That is decided from the profile's structure, not from its name.

**Seeing what happened**
* **A self contained HTML report**: sortable tables, collapsible sections and inline charts, with no external stylesheet, script, font or image. It opens straight off disk, and renders the same offline, on a television, or as an email attachment.
* **A CSV export** of the same data, for a spreadsheet.
* **Emailed reports**, optionally, delivered by the [Newsflasharr](https://github.com/PiratesIRC/Dispatcharr_Newsflasharr) plugin. Off by default.
* **A loud banner when the numbers cannot be trusted**, rather than a clean looking report claiming your household watches nothing.

See the **[user guide](docs/USER-GUIDE.md)** for how to use these, and for every setting and every button.

### The report

The page opens as an index: the logo beside the title, the tracking window and sampling verdict, then seven sections that each start collapsed and carry their own count.

![The Dustarr report as it opens: the logo beside the title, the tracking window and coverage on one line, a Report number chip, a four segment bar splitting the judged channels into never watched, watched, too new and tuned but never qualified, a sampling density meter with a sampling OK verdict beside it, and seven collapsed section headings each carrying a count](docs/images/report-index.png)

Click a heading and its tables appear.

![The same report with the Never watched section expanded, showing a per group rollup table with a share bar, and below it a sortable table of seven channels with their group, watch count, hours, average minutes, tune count, age in days, days since last watch, last watched date and reason](docs/images/report-expanded.png)

*Sample data, shown here in dark mode; the page follows whichever theme your reader uses. Click any column heading to sort by it. Numeric columns compare as numbers, so 9 sorts before 10 rather than after 100. The chip under the title is the running total of reports this installation has written.*

## Requirements

* Dispatcharr v0.20.0+
* No internet access of any kind. The plugin never contacts your provider, never checks for its own updates, and fetches nothing when rendering a report.
* The [Newsflasharr](https://github.com/PiratesIRC/Dispatcharr_Newsflasharr) plugin, only if you want emailed reports. It is what actually sends the mail. Dustarr does not require it: with Newsflasharr absent or disabled, nothing is sent and nothing fails.

## Installation

1. Log in to Dispatcharr's web UI.
2. Navigate to **Plugins**.
3. Click **Import Plugin** and upload the plugin zip file.
4. Enable the plugin after installation.

Upgrading has its own short procedure, and it does need a container restart: see [Updating the plugin](docs/USER-GUIDE.md#updating-the-plugin).

## What to expect at first

**Your first month of reports will carry a red "not trustworthy" banner, and that is the plugin working rather than failing.** The unused threshold defaults to 30 days, and a dataset younger than that cannot honestly call anything unused. There is no way to shorten this by importing history: Dispatcharr does not keep the state Dustarr reads, so a fresh installation genuinely starts at zero. Wait it out, or lower the threshold if you are comfortable judging on a shorter window.

**Most of a typical lineup is excluded from judgment, and that is the point.** On a normal installation roughly 70 percent of channels are held back for the reasons listed above. The actionable answer is drawn from the remaining 30 percent, not from the whole lineup.

## Safety

This is the part worth reading before installing anything that watches what your household watches.

* **It never writes to Dispatcharr's database.** That is not a promise in a README. `tests/test_no_mutations.py` reads the syntax tree of every shipped module on every run and fails the build on any write shaped database call it can prove, on `subprocess` and its relatives, and on any stream probe. It proves the receiver is a Dispatcharr model or queryset rather than banning method names, so it is not fooled by a dictionary that happens to have an `update` method. What it cannot see: a call made through reflection or `eval`, a queryset arriving as a function parameter, or a write issued through a library it does not know about. It is a strong structural guarantee against the natural ways an author would reach for a write, not a formal proof.
* **It never contacts your provider.** No stream requests and no probes. A single probe consumes one of your provider connections and drops whoever is currently watching.
* **Credentials are redacted.** Provider credentials live inside stream URLs in a typical Dispatcharr setup, so every string that can reach a notification or a logged error passes through a redactor first, and the report renders an allowlisted set of fields only: channel name, group, counts and timestamps, never a stream URL.
* **Nothing is served over HTTP.** The report is a file in a folder you already have mounted. An earlier version wrote it where Dispatcharr's own web server publishes files to the whole local network with no login, which made an unauthenticated page naming every channel your household watches.
* **If the collector goes blind, the report says so loudly**, rather than quietly reporting that every channel is dead.

## Where things are written

The report and the CSV land in `/config/dustarr/`. `/config` is Dispatcharr's existing bind mount, so that is a real folder on your host: open the report by double clicking it. The full list of paths is in the [user guide](docs/USER-GUIDE.md#where-the-files-are-written).

## Documentation

* **[User guide](docs/USER-GUIDE.md)**: a first run, reading the report, every setting, every button.
* **[Troubleshooting](docs/troubleshooting.md)**: arranged by symptom.
* **[Changelog](docs/CHANGELOG.md)**: what changed in each version.
* **[Development notes](docs/DEVELOPMENT.md)**: how the plugin is put together, and the constraints that are not obvious from one file.
* **[Open work](docs/TODO.md)**: what is planned, what is deliberately not, and the known limitations.
* **[Contributing](CONTRIBUTING.md)** before opening an issue or a pull request, and **[Security](SECURITY.md)** for what the plugin can reach and how to report a vulnerability privately.

## Versioning

Calendar versioning, `Major.YY.DDDHHMM`: major version, two digit year, day of year, then the UTC time the version was cut. `1.26.2241505` is major version 1, built on day 224 of 2026 at 15:05 UTC. A later version string is always a later build.

## Sponsor

This plugin is free and always will be. If it saves you time and you would like to support the work, you can sponsor it at [github.com/sponsors/PiratesIRC](https://github.com/sponsors/PiratesIRC).

Sponsoring buys no priority, no private support and no influence over what gets built. Bug reports and pull requests are just as welcome from everyone.

## License

MIT. See [LICENSE](LICENSE).
