"""Metricsarr gateway — the ONLY ORM reader. READ-ONLY BY CONSTRUCTION.

Phase 1 mutates nothing: there is no .save()/.update()/.delete()/.create() in this
file, and tests/test_no_mutations.py enforces that structurally (spec S11.1).

Django imports are function-local: module-level imports break the plugin loader.
"""
from __future__ import annotations

import re
import time
from collections import namedtuple

ChannelRow = namedtuple(
    "ChannelRow", "id uuid name group auto_created created_at proxying")

# The 391 auto-created channels on this box are exactly US: PPV + 24/7 Streams.
# LIVE EVENT slots are PERSISTENT rows that M3U sync RENAMES -- one idled on
# "NO EVENT" for 34 days and would read as unused (product #4).
DEFAULT_EXCLUDE_GROUPS = ("US: PPV, US: STL, US: News, US: NBC, US: ABC, "
                          "US: CBS, US: FOX, US: Sports")
DEFAULT_EXCLUDE_NAME_RE = r"(?i)(LIVE EVENT|PPV|NO EVENT|24/7)"


def parse_exclusions(settings):
    settings = settings or {}

    # Defaults keep their real-world casing ("US: PPV", matching Dispatcharr's
    # actual group names) so a caller inspecting ex["groups"] sees the literal
    # configured value; user-supplied lists are normalized (stripped + lowered)
    # for typo/case tolerance. classify() lower-cases both sides at match time,
    # so either form matches correctly regardless of casing.
    if "exclude_groups" in settings:
        raw_groups = settings.get("exclude_groups")
        groups = {g.strip().lower() for g in str(raw_groups or "").split(",")
                  if g.strip()}
    else:
        groups = {g.strip() for g in DEFAULT_EXCLUDE_GROUPS.split(",") if g.strip()}

    raw_re = settings.get("exclude_name_regex", DEFAULT_EXCLUDE_NAME_RE)
    name_re = None
    if raw_re:
        try:
            name_re = re.compile(str(raw_re))
        except re.error:
            # A user's broken regex must degrade to "no name rule", never wedge
            # the report.
            name_re = None

    return {
        "exclude_auto_created": bool(settings.get("exclude_auto_created", True)),
        "groups": groups,
        "name_re": name_re,
    }


def classify(row, exclusions, now):
    """Return an exclusion/observability reason, or None if the channel counts.

    Explicit exclusions are reported ahead of observability so the report's
    `reason` column names the rule the user actually configured.
    """
    if exclusions["exclude_auto_created"] and row.auto_created:
        return "excluded:auto_created"
    if row.group:
        group = row.group.strip().lower()
        if any(group == excl.strip().lower() for excl in exclusions["groups"]):
            return "excluded:group"
    if exclusions["name_re"] and row.name and exclusions["name_re"].search(row.name):
        return "excluded:name"
    if not row.proxying:
        return "unobservable"
    return None


class DjangoGateway:
    """Read-only ORM access. One bulk query; no N+1."""

    def now(self):
        return time.time()

    def channels(self):
        from apps.channels.models import Channel  # Dispatcharr runtime only

        rows = []
        queryset = (Channel.objects
                    .select_related("channel_group", "stream_profile")
                    .only("id", "uuid", "name", "channel_group__name",
                          "stream_profile__name", "auto_created", "created_at"))
        for channel in queryset.iterator(chunk_size=500):
            group = getattr(channel.channel_group, "name", None)
            rows.append(ChannelRow(
                id=channel.id,
                uuid=str(channel.uuid),
                name=channel.name or "",
                group=group,
                auto_created=bool(getattr(channel, "auto_created", False)),
                created_at=_epoch(getattr(channel, "created_at", None)),
                proxying=_is_proxying(channel),
            ))
        return rows


def _epoch(value):
    if value is None:
        return None
    try:
        return value.timestamp()
    except AttributeError:
        return float(value)


def _is_proxying(channel):
    """A non-proxying (Redirect) profile never writes live:channel:* keys, so the
    channel is invisible to the collector. Default profile (None) proxies."""
    profile = getattr(channel, "stream_profile", None)
    if profile is None:
        return True
    name = (getattr(profile, "name", "") or "").strip().lower()
    return name not in ("redirect", "proxy off", "direct")
