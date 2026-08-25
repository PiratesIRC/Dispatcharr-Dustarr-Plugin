# Reading Dustarr's published-report count

Dustarr publishes a running total of the reports it has successfully written, so
that another tool can display it, for example as a badge. This page is the
specification for anything that wants to read that number.

You do not need this page to use Dustarr. It is here for people writing an
integration.

## The contract

A JSON file in Dustarr's own data directory, rewritten whenever a report is
successfully published.

```
/data/dustarr/report_count.json
```

```json
{"reports_built": 42}
```

That is the whole contract. One key, one non-negative integer.

**What the number counts:** reports whose HTML file was confirmed written to
disk. It increments once per successful build, whether an operator pressed the
**Build report** button or the scheduled job ran. A build that failed to write
does not increment it, which matters more than it sounds: the report writer
degrades rather than raising, so a failed publish otherwise looks exactly like a
successful one.

**Guarantees:**

- Monotonically increasing across the life of the data directory.
- Written atomically, to a temporary file and then renamed over the old one, so
  a reader never sees a partial file.
- Owned by `dispatch:dispatch`, mode 644, readable by any process in the
  container.

**Explicit non-guarantees:**

- **It can undercount.** The increment is a read followed by a write with no
  lock, and Dispatcharr runs several web and background workers. Two reports
  finishing in the same instant can lose one increment. Holding a file lock on
  the request path is a worse trade than an occasional undercount in a number
  that is displayed and never acted on. Reports are minutes apart in practice.
- **It is not a delivery count.** It counts reports that exist, not reports that
  were emailed. With notifications off it still increments.
- **It resets if `/data/dustarr/` is cleared.** It is not backed by a database.

## Why the number is not in the notification payload

The obvious design is for Dustarr to put the number in the event it already
sends to the [Newsflasharr](https://github.com/PiratesIRC/Dispatcharr_Newsflasharr)
notification plugin. That does not work, and the reason is worth stating so it
does not get re-proposed.

The shared notification client's `notify()` function takes a **closed set of
keyword arguments**: `source`, `title`, `event`, `body`, `severity`, `kind`,
`dedup_key`, `url`, `attachment` and `base_dir`. It writes a fixed message
schema built from exactly those. There is no field for arbitrary data.

Adding one is not a local change. That client is vendored byte for byte into
several separate plugins and hash-pinned by each one's `client_manifest.json`.
Changing it means editing the shared source, re-vendoring it everywhere,
regenerating every hash pin, and getting every one of those repositories green,
for a cosmetic counter.

Dustarr does put a human-readable line in the notification body:

```
report number 42
```

**Do not parse that.** It is there so that a person reading the mail sees the
number. Prose in a message body is not a contract and would break the first time
the wording changed.

## Reading it

```python
import json
import os


def read_source_report_count(source, data_root="/data"):
    """Return the number of reports `source` has published, or 0.

    Degrades to 0 on anything unexpected. A badge must never invent activity,
    and must never be the reason a page fails to render.
    """
    path = os.path.join(data_root, source, "report_count.json")
    try:
        with open(path, encoding="utf-8") as fh:
            value = int(json.load(fh).get("reports_built", 0))
        return value if value >= 0 else 0
    except Exception:
        return 0
```

## Covering more than one plugin

The path is `/data/<source>/report_count.json`, where `<source>` is the same
string the plugin sends as `source` in its notifications. A notification plugin
that already records which sources have sent reports therefore already knows
which paths to probe, and needs no per-plugin special casing. A source with no
such file reads 0 and can be left out.

**Only Dustarr writes this file today.** Any other plugin that wants to be
counted has to write it too.

Two cheaper approaches are worth considering before building this at all:

1. **Count an existing notification ledger.** A notification plugin that already
   writes one line per event, with a `source` field, can count them in one pass
   over a file it already owns, with no cross-plugin contract. The catch: that
   counts events **delivered**, not reports **built**, so it undercounts
   whenever notifications are off or a send fails, and it depends on the ledger
   never being trimmed. Check the retention policy before relying on it.
2. **Report the last publication time instead.** If "when was the last report"
   answers the real question, a notification plugin already has that timestamp
   and no new code is needed anywhere.

## Verifying an integration

Read the file, press Dustarr's **Build report** button, and read it again. The
number must increase by exactly one, and a new report file must appear in
`/config/dustarr/`.

Do not verify by watching a notification arrive, which proves delivery rather
than publication. Do not verify by the button returning green, which is the
exact failure this counter's publish check exists to avoid.
