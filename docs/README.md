# Dustarr documentation

Six pages, split by who is reading.

## If you are running Dispatcharr

**[User guide](USER-GUIDE.md)** is the one you want. It covers a first run, what
the report actually contains, what the red "not trustworthy" banner means and
why you should expect it for the first month, where every file is written, and
the complete settings and actions reference. It also explains why most used and
least used deliberately leave out your excluded groups.

**[Troubleshooting](troubleshooting.md)** is arranged by symptom: no report
arrived, nothing is being recorded, the numbers look wrong, the email is not
turning up.

**[Changelog](CHANGELOG.md)** lists what changed in each version, described in
terms of what you will notice rather than which functions moved.

## If you are working on Dustarr itself

**[Development notes](DEVELOPMENT.md)** cover the runtime model, which is the
first thing to understand: the plugin runs inside Dispatcharr's Django backend
and cannot be run standalone. They also cover the module layout, which modules
are deliberately stdlib only and why, what each test file pins, and the
constraints that are not obvious from reading one file.

**[Open work](TODO.md)** is the backlog: what is done, what is planned, what is
deliberately not being done, and the measurement behind each item.

## If you are building something that reads Dustarr's output

**[Newsflasharr report count specification](newsflasharr-report-count-spec.md)**
describes the one file Dustarr publishes for another tool to read,
`/data/dustarr/report_count.json`, and what the Newsflasharr plugin would need
in order to turn it into a badge. It exists because the shared notification
client has a fixed payload with no field for extra data, so nothing structured
can travel that path.

---

Two rules govern everything written here and everything the plugin renders.

**The plugin never writes to Dispatcharr's database.** That is enforced by a
test that reads the syntax tree of every shipped module on every run, not by a
promise in a document. `docs/DEVELOPMENT.md` explains what it can and cannot
prove.

**A measurement that could not be taken is never reported as a clean result.**
The report says loudly when it does not trust its own numbers, the report
counter renders nothing rather than zero when it cannot be read, and the
schedule is judged by a timestamp the scheduled task writes rather than by a
counter that advances whether or not the task ran.
