---
name: Bug report
about: Something the plugin does that it should not, or does not do that it should
title: ''
labels: ''
assignees: ''

---

**Please do not paste provider credentials, stream URLs, M3U account names or
server addresses.** A stream URL carries your username and password in its path,
and an M3U account name is often your provider's hostname. Channel names are
fine, and are usually needed. Your full channel list is not: quote the few
channels that matter rather than attaching the CSV export.

**What happened**

<!-- What you saw. Include the exact text of any error shown on the plugin card. -->

**What you expected instead**

**Steps to reproduce**

1.
2.
3.

**What Validate settings reported**

<!--
Press Validate settings on the plugin's Actions tab and paste the result. It
writes nothing, and it names the problem in most cases: whether every setting
parses, whether the collector is running, whether the scheduled report exists
and is queued to a worker that will actually run it, and whether email can go
out.
-->

**Versions**

- Plugin version (from the plugin card):
- Dispatcharr version:
- How Dispatcharr is running (Docker image, bare metal, other):

**Which part is affected**

<!-- Tick what applies. -->

- [ ] The collector, meaning viewing is not being recorded at all
- [ ] The report contents, meaning the numbers look wrong
- [ ] The report page itself, meaning layout, charts or something not rendering
- [ ] The schedule, meaning the weekly or daily report did not arrive
- [ ] Email delivery through Newsflasharr
- [ ] Something else

**Anything else**

<!--
If the report looks wrong, say how long the plugin has been collecting. The
report refuses to call anything unused until the dataset is older than the
unused threshold, 30 days by default, and it says so at the top of the page.
That banner is the age gate working rather than a fault.
-->
