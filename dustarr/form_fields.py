"""Settings form layout for Dispatcharr 0.34 and later. Stdlib only.

Dispatcharr 0.34.0 added collapsible ``section`` fields and ``multiselect`` pick
lists. Both need ``manifest_version`` 1 in ``plugin.json``, and Dispatcharr 0.33
and earlier drop a field of a type they do not know. So the plugin keeps its
flat form in ``Plugin.fields``, which every version reads, and builds the
sectioned copy here for ``Plugin.get_fields``, which only 0.34 and later call.

The 0.34 form draws ``exclude_groups`` as a pick list under a NEW id,
``exclude_group_list``: a multiselect calls ``.map`` on its value, so a stored
comma-separated string under the old id would break the form. The old id keeps
its text value for 0.33, and ``legacy_settings`` writes a list saved under the
new id back into the old id at run time, so every reader sees one text form.
"""
from __future__ import annotations

import copy

# new id: old id
LIST_SETTINGS = {"exclude_group_list": "exclude_groups"}
NEW_ID_BY_OLD_ID = {old: new for new, old in LIST_SETTINGS.items()}

NEW_FIELDS = {
    "exclude_group_list": {
        "label": "Excluded groups",
        "help_text": "Channel groups that are never judged unused. Local, OTA and "
                     "news are the emergency tier, and sports has a legitimate "
                     "off-season. The list holds the groups that contain at least "
                     "one channel, read when the plugin loads: after adding a new "
                     "group, press the refresh control on the Plugins page to see "
                     "it here.",
    },
}

SECTION_PREFIX = "_section_"
ABOUT_SUFFIX = "_about"
HELP_SUFFIX = "_help_"

# Groups a typical user rarely opens start closed.
COLLAPSED_SECTIONS = frozenset(("_section_collection", "_section_usage"))

# Dispatcharr draws help text as one plain paragraph. In the 0.34 form a long
# help text is cut to a short form and the rest follows as titled info fields
# right below the field. The flat form keeps the full text.
HELP_PARAGRAPHS = {
    "never_watched_ceiling": (
        "Fraction of judged channels that must look never-watched before the "
        "data is treated as untrustworthy, because the collector is probably "
        "blind.",
        [
            ("What counts as judged",
             "Judged channels are the never-watched, too-new, "
             "tuned-but-never-qualified and watched ones. Excluded and "
             "unobservable channels do not count."),
            ("Why the default is high",
             "Most real lineups exclude most channels by policy, so a healthy "
             "household can easily show 80 to 90 percent never-watched among "
             "the rest. The high default only catches the shape where "
             "essentially every judged channel looks dead."),
        ],
    ),
    "report_retention_days": (
        "After each report is built, its own report-<stamp>.html and "
        "report-<stamp>.csv files older than this many days are deleted. 0 "
        "keeps every file, which is the default.",
        [
            ("What is never deleted",
             "The report just written is never deleted, at least one file of "
             "each kind always survives, and the live report.html is never "
             "touched. Only files this plugin wrote are considered."),
            ("The existing cap",
             "This is on top of the existing cap that keeps the newest few "
             "files of each kind regardless of age."),
        ],
    ),
}


def is_section_header(field):
    """True for an ``info`` field whose id marks the start of a settings group."""
    return (isinstance(field, dict) and field.get("type") == "info"
            and str(field.get("id", "")).startswith(SECTION_PREFIX))


def split_list(value):
    """A comma-separated setting, or a list, as trimmed non-empty strings."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def legacy_settings(settings, enabled):
    """Return a copy of ``settings`` with every list value saved under a new id
    written back into its old text setting.

    Only a new id holding a list counts. ``enabled`` is
    ``gateway.fields_api_available()`` at the call site: False on Dispatcharr
    0.33 and earlier, where the settings come back unchanged, so a list left
    over from a downgrade never overrides the text the operator now edits."""
    out = dict(settings) if isinstance(settings, dict) else {}
    if not enabled:
        return out
    for new_id, old_id in LIST_SETTINGS.items():
        value = out.get(new_id)
        if isinstance(value, list):
            out[old_id] = ", ".join(split_list(value))
    return out


def _options(names, extra=()):
    """Sorted options from ``names`` plus every ``extra`` value, deduplicated
    ignoring case (first spelling wins). A name holding a comma is left out,
    because the text form it is written back into could not hold it. An
    ``extra`` value is never left out: Dispatcharr rejects a save holding a
    value that is not an option."""
    seen = {}
    for name in names or ():
        name = str(name or "").strip()
        if name and "," not in name:
            seen.setdefault(name.casefold(), name)
    for name in extra or ():
        name = str(name or "").strip()
        if name:
            seen.setdefault(name.casefold(), name)
    return [{"label": n, "value": n} for n in sorted(seen.values(), key=str.casefold)]


def _canonical(values, names):
    """Each value spelled as the matching name (ignoring case), else as typed."""
    by_key = {str(n).strip().casefold(): str(n).strip() for n in names or ()
              if str(n or "").strip()}
    return [by_key.get(v.casefold(), v) for v in values]


def pick_list_fields(flat_fields, settings=None, group_names=()):
    """Return the flat form with each text setting in ``LIST_SETTINGS`` replaced
    by a multiselect under its new id. The input is not modified.

    The default is built from the saved old text setting (or its field
    default), so an install that never saved the new field keeps its values."""
    settings = settings if isinstance(settings, dict) else {}
    out = []
    for field in flat_fields or []:
        if not isinstance(field, dict) or field.get("id") not in NEW_ID_BY_OLD_ID:
            out.append(copy.deepcopy(field) if isinstance(field, dict) else field)
            continue
        new_id = NEW_ID_BY_OLD_ID[field["id"]]
        meta = NEW_FIELDS[new_id]
        default = _canonical(split_list(settings.get(field["id"], field.get("default", ""))),
                             group_names)
        stored = settings.get(new_id)
        stored = split_list(stored) if isinstance(stored, list) else []
        out.append({
            "id": new_id, "label": meta["label"], "type": "multiselect",
            "default": default, "options": _options(group_names, default + stored),
            "help_text": meta["help_text"],
        })
    return out


def sectioned_fields(flat_fields):
    """Return the flat form rearranged into collapsible sections.

    Each section header becomes a ``section`` marker with the same id, and every
    field after it, up to the next header, gets ``"section": <that id>``. The
    header's text moves to an ``info`` field ``<id>_about`` at the top of its
    section, because a marker shows only its label. The input is not modified."""
    out = []
    current = None
    for field in flat_fields or []:
        if not isinstance(field, dict):
            continue
        if is_section_header(field):
            current = field["id"]
            out.append({"id": current, "label": field.get("label") or current,
                        "type": "section", "collapsed": current in COLLAPSED_SECTIONS})
            text = field.get("description") or field.get("help_text")
            if text:
                out.append({"id": current + ABOUT_SUFFIX, "label": "", "type": "info",
                            "description": text, "section": current})
            continue
        item = copy.deepcopy(field)
        if current is not None:
            item["section"] = current
        out.append(item)
        out.extend(_split_help(item, current))
    return out


def _split_help(item, section):
    """Cut a long help text to its short form (in place) and return the rest as
    titled info fields that follow the field."""
    spec = HELP_PARAGRAPHS.get(item.get("id"))
    if not spec:
        return []
    short, paragraphs = spec
    key = "help_text" if "help_text" in item else "description"
    item[key] = short
    extra = []
    for number, (title, text) in enumerate(paragraphs, start=1):
        info = {"id": f"{item['id']}{HELP_SUFFIX}{number}", "label": title,
                "type": "info", "description": text}
        if section is not None:
            info["section"] = section
        extra.append(info)
    return extra


def build_form(flat_fields, settings=None, group_names=()):
    """The full 0.34 form: pick lists first, then sections."""
    return sectioned_fields(pick_list_fields(flat_fields, settings, group_names))
