# Reading Dustarr's published-report count

A specification for the **Newsflasharr** side of a "reports built" badge.
Nothing in this document has been implemented in Newsflasharr. Dustarr's half
is done and deployed; this describes what the other plugin would need to do.

Written from Dustarr, which is a separate project. The workspace rule is that
one project does not write to another, so this is a handoff rather than a
change.

## What Dustarr provides

A JSON file in its own data directory, written by the plugin whenever a report
is successfully published.

```
/data/dustarr/report_count.json
```

```json
{"reports_built": 42}
```

That is the whole contract. One key, one non-negative integer.

**What the number counts:** reports whose HTML file was confirmed written to
disk. It increments once per successful build, whether the operator pressed
the `Build report` button or Celery Beat ran the weekly job. A build that
failed to write does not increment it, which matters because Dustarr's report
writer degrades rather than raising, so a failed publish otherwise looks
exactly like a successful one.

**Guarantees:**

- Monotonically increasing across the life of the data directory.
- Written atomically (temporary file, then `os.replace`), so a reader never
  sees a partial file.
- Owned by `dispatch:dispatch`, mode 644, readable by any process in the
  container.

**Explicit non-guarantees:**

- **It can undercount.** The increment is a read-modify-write with no lock,
  and Dispatcharr runs several uWSGI and Celery workers. Two reports finishing
  in the same instant can lose one increment. A lock spanning file I/O on the
  request path is a worse trade than an occasional undercount in a cosmetic
  number. Reports are minutes apart in practice.
- **It is not a delivery count.** It counts reports that exist, not reports
  that were emailed. With notifications off it still increments.
- **It resets if `/data/dustarr/` is cleared.** It is not backed by the
  database.

## Why not the notification payload

The obvious design is for Dustarr to put the number in the event it already
sends Newsflasharr. That does not work, and the reason is worth recording so
nobody re-proposes it.

`notify_client.notify()` takes a **closed set of keywords**: `source`,
`title`, `event`, `body`, `severity`, `kind`, `dedup_key`, `url`,
`attachment`, `base_dir`. It writes a fixed spool schema built from exactly
those. There is no field for arbitrary data.

Adding one is not a local change. `notify_client.py` is vendored
**byte-identically into five projects** and hash-pinned by each one's
`client_manifest.json`. Changing it means editing the shared source,
re-vendoring into all five, regenerating five hash pins, and getting five CI
runs green, for a cosmetic counter.

Dustarr does put a human-readable line in the notification body:

```
report number 42
```

**Do not parse that.** It exists so a person reading the mail sees the number.
Prose in a body is not a contract, and it would break the first time the
wording changed.

## Suggested Newsflasharr implementation

The shape below is a suggestion from outside the project. Its maintainer
should adapt it.

### Reading

```python
import json
import os

def read_source_report_count(source, data_root="/data"):
    """-> int. The number of reports `source` has published, or 0.

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

### Generalising past Dustarr

The path is `/data/<source>/report_count.json`, where `<source>` is the same
string the plugin already sends as `source` in its notifications. Newsflasharr
already keys `report_last` in its `state.json` on `<source>/<event>`, so it
already knows the set of sources that send reports:

```json
"report_last": {
  "channel-mapparr/usage_report": 1785699612.49,
  "lineuparr/usage_report": 1785713161.395,
  "dustarr/usage_report": 1785928089.656
}
```

Walking those keys and probing each `/data/<source>/report_count.json` gives a
count per source with no per-plugin special casing, and no change in any
plugin that has not opted in: a source with no such file reads 0 and can
simply be omitted from the badge.

**Only Dustarr writes this file today.** If the badge is meant to cover other
plugins, each one needs the same 30 lines, and each is its own project needing
its own sign-off.

### Two cheaper alternatives worth considering first

1. **Count the ledger.** `/data/newsflasharr/notifications.jsonl` already has
   one line per event with a `source` field, so a count is one pass over a
   file Newsflasharr already owns, with no cross-plugin contract at all. The
   catch: it counts events **delivered**, not reports **built**, so it
   undercounts whenever notifications are off or a send fails, and it depends
   on the ledger never being trimmed. Confirm the retention policy before
   relying on it.

2. **Serve `report_last` alone.** If "when was the last report" answers the
   real question, Newsflasharr already has it in `state.json` and no new code
   is needed anywhere.

## Verifying it works

Read the file, press Dustarr's `Build report` button, read it again. The
number must increase by exactly one, and a second report file must appear in
`/config/dustarr/`. Do not verify by watching the notification arrive, which
proves delivery rather than publication, and do not verify by the button
returning green, which is the exact failure this counter's publish check
exists to avoid.
