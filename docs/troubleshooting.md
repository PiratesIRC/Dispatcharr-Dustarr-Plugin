# Troubleshooting

Start with **Validate settings** on the plugin's Actions tab. It writes nothing,
and it names the problem in most of the cases below: whether every setting
parses, whether the collector is running, whether the scheduled report exists
and is queued to a worker that will actually run it, and whether email can go
out.

One thing it cannot tell you, and it is worth knowing why: **Validate settings
can never report a missing schedule.** Every action re-creates the schedule
before it runs, so by the time the check looks, the schedule is there. What it
does report is how long ago the scheduled report last completed, which is the
number that actually answers the question.

---

## The scheduled report did not arrive

**Check the age first.** Validate settings reports when the scheduled report
last ran. If that is longer ago than your schedule interval, the schedule did
not fire; if it is recent, the report was built and the problem is delivery.

Things that stop it firing:

- **The schedule was cancelled by a plugin reload.** Versions before
  `1.26.2241505` deleted the scheduled job whenever the plugin was stopped, and
  Dispatcharr stops every plugin when the Refresh control on the Plugins page is
  pressed, not only on disable or uninstall. Nothing re-created it until an
  action button was next clicked, so the report silently stopped arriving while
  the plugin card, the version and the collector all looked normal. **Upgrade,
  and press Validate settings once to re-arm the schedule.**
- **The scheduled report is set to Off.** Check the Scheduled report setting.
- **The job is queued to a worker that will not run it.** Dispatcharr registers
  a plugin's scheduled task on only one of its background workers. Validate
  settings checks this and says so explicitly if it is wrong.

Things that stop it being delivered are in the email section below.

## The report says the data is not trustworthy

This is the age gate working, not a fault. The report refuses to call anything
unused until the dataset is older than the **Unused threshold** setting, which
is 30 days by default. Until then it says so at the top of the page and keeps
collecting. Nothing needs doing except waiting.

The other honesty gate is the never-watched ceiling. If almost every judged
channel looks never-watched, the report assumes the collector was blind rather
than that your whole lineup is dead, and says so. On a normal lineup a high
never-watched percentage is expected and is not what trips this.

## Nothing is being recorded at all

The collector runs inside Dispatcharr's web workers. It starts when the plugin
is constructed, which happens when Dispatcharr loads plugins.

- **After upgrading the container, the collector starts again on its own.** If
  you are unsure whether it is running, Validate settings reports it.
- **Viewing is recorded only while somebody is actually watching.** The
  collector samples which channels have viewers; it cannot reconstruct history.
  A channel watched before the plugin was installed is invisible to it.
- **A watch has to last at least the Minimum watch setting**, 120 seconds by
  default, before it counts as a watch. Shorter than that is recorded
  separately as a tune, which is a different and important category. See below.

## The report lists a channel as never watched, but I watch it

Two likely causes, and they mean opposite things.

- **It is in the tuned-but-never-qualified list instead.** That means the
  channel was tuned repeatedly and every attempt ended inside the minimum watch
  time. Those channels are usually **broken**, not unused. Turning them off is
  the opposite of what you want.
- **You watch it outside Dispatcharr.** The plugin sees only what flows through
  Dispatcharr's own proxy.

## Most of my channels are listed as never watched

That is normal and is usually correct. A typical provider lineup carries
thousands of channels and a household watches a few dozen. The point of the
plugin is to name the rest.

Note that the **Most used** and **Least used** rankings are drawn only from the
channels the plugin is willing to judge. News, over-the-air, sports and
auto-created channels are excluded by default, so a channel you watch often can
be missing from the rankings entirely. The report says so under both sections.
Those exclusions protect the emergency tier and the event slots that a provider
renames in place, so relaxing them is rarely the right fix.

## The email is not arriving

The report is always written to disk first, so a mail problem never costs you
the report. Look in `/config/dustarr/` regardless.

For the mail itself, Build report names anything missing. The usual causes:

- **Notifications are switched off** in the plugin settings.
- **The Newsflasharr plugin is missing, disabled, or its outgoing mail is not
  fully configured.**
- **Newsflasharr has no routing rule sending this plugin's notifications to
  mail.** This is the case worth understanding: the notification is accepted and
  queued successfully, and it is then delivered somewhere else or nowhere,
  which looks exactly like success from this side. The routing rule matches on
  the source name `dustarr` and the event name `usage_report`.

## Where the files are

- **Report and CSV export:** `/config/dustarr/`, which is a directory on the
  host, so you can open the report by double-clicking it. Both a timestamped
  copy and a stable `report.html` are written.
- **Collected viewing data:** `/data/dustarr/usage.json`.
- **Nothing is served over HTTP**, deliberately. Earlier versions wrote the
  report where Dispatcharr's web server served it to the whole local network
  without authentication.

Open the timestamped `report-<date>-<time>.html` rather than `report.html` when
checking whether a new report was written. A stale cached copy of the stable
filename can otherwise look like nothing happened.

## Still stuck

Open an issue on the
[issue tracker](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/issues).
Please do not paste provider credentials, stream URLs, M3U account names or
server addresses: a stream URL carries your username and password in its path.
