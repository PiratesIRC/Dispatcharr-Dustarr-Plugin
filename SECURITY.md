# Security

## Reporting a vulnerability

Please report security issues privately using GitHub's
[private vulnerability reporting](https://github.com/PiratesIRC/Dispatcharr-Dustarr-Plugin/security/advisories/new)
rather than opening a public issue.

Include what you observed, the plugin version shown on the plugin card, and the
steps to reproduce it. **Do not include your provider credentials, stream URLs,
or M3U account names**: a stream URL carries your username and password in its
path, and an M3U account name is often your provider's hostname.

## What this plugin has access to, and what it does with it

Dustarr is **read-only with respect to Dispatcharr**. It never creates, updates
or deletes a channel, a stream, an assignment or a setting. That is enforced by
a test, not by convention: `tests/test_no_mutations.py` walks the syntax tree of
every shipped module and fails the build on a write-shaped database call. The
guard decides by proving what the call is made on, so it can allow a write to a
dictionary or a set while still refusing a write to a Dispatcharr model, and it
fails when it cannot prove the receiver rather than passing.

What it does touch:

- **Reads** Dispatcharr's channel, group and M3U account records to build the
  list of channels it can judge.
- **Reads** Dispatcharr's Redis keys to see which channels currently have
  viewers. It polls; it never opens a provider connection and never runs
  ffmpeg or ffprobe.
- **Writes only its own files**: viewing data in `/data/dustarr/`, and the
  report and CSV export in `/config/dustarr/`.
- **Sends a notification** through the Newsflasharr plugin when notifications
  are enabled, with the report attached.

## Things worth knowing before you report

- **The report contains channel names and viewing counts, and nothing else
  about your provider.** It carries no stream URLs, no M3U account names and no
  server addresses. It is written to be safe to email to yourself; it is not
  written to be safe to publish, because your channel lineup identifies your
  subscription.
- **Nothing is served over HTTP.** Earlier versions wrote the report into
  Dispatcharr's logo directory, which Dispatcharr's own web server serves to the
  whole local network without authentication. That was an unauthenticated
  listing of every channel the household watches. Since `1.26.2171040` the
  report is written only to `/config/dustarr/`, and three tests exist to stop it
  being moved back.
- **Values that look like credentials are redacted before they reach a log, a
  notification or an error message**, by `dustarr/redaction.py`. The redaction
  is scoped to text that reads as a media URL and is deliberately not applied to
  free text, because a bare-hostname rule produces false matches on ordinary
  words.
- **The plugin makes no outbound network request of its own.** The only thing
  that leaves the machine is the notification you configured, sent by
  Newsflasharr through the channel you configured.

## Supported versions

The latest version is the supported one. Fixes are made on `master` and shipped
in the next version rather than backported.
