"""Dustarr gateway — the ONLY ORM reader. READ-ONLY BY CONSTRUCTION.

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

# On the reference installation the 391 auto-created channels are exactly
# US: PPV + 24/7 Streams.
# LIVE EVENT slots are PERSISTENT rows that M3U sync RENAMES -- one idled on
# "NO EVENT" for 34 days and would read as unused (product #4).
DEFAULT_EXCLUDE_GROUPS = ("US: PPV, US: STL, US: News, US: NBC, US: ABC, "
                          "US: CBS, US: FOX, US: Sports")
DEFAULT_EXCLUDE_NAME_RE = r"(?i)(LIVE EVENT|PPV|NO EVENT|24/7)"


def parse_exclusions(settings):
    settings = settings or {}

    # Always lower-case for consistent O(1) set lookups in classify().
    groups = {g.strip().lower() for g in str(settings.get("exclude_groups", DEFAULT_EXCLUDE_GROUPS) or "").split(",")
              if g.strip()}

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
    if row.group and row.group.strip().lower() in exclusions["groups"]:
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

        # I3: resolve the GLOBAL default stream profile ONCE per report run
        # (a single extra read), not per row -- see
        # _default_stream_profile_is_redirect.
        default_is_redirect = _default_stream_profile_is_redirect()

        rows = []
        # stream_profile__locked is fetched because StreamProfile.is_redirect()
        # reads it; leaving it deferred would trigger one extra query per row
        # that carries an explicit profile.
        queryset = (Channel.objects
                    .select_related("channel_group", "stream_profile")
                    .only("id", "uuid", "name", "channel_group__name",
                          "stream_profile__name", "stream_profile__locked",
                          "auto_created", "created_at"))
        for channel in queryset.iterator(chunk_size=500):
            group = getattr(channel.channel_group, "name", None)
            rows.append(ChannelRow(
                id=channel.id,
                uuid=str(channel.uuid),
                name=channel.name or "",
                group=group,
                auto_created=bool(getattr(channel, "auto_created", False)),
                created_at=_epoch(getattr(channel, "created_at", None)),
                proxying=_is_proxying(channel, default_is_redirect),
            ))
        return rows


def _epoch(value):
    if value is None:
        return None
    try:
        return value.timestamp()
    except AttributeError:
        return float(value)


_NON_PROXYING_NAMES = ("redirect", "proxy off", "direct")


def _default_stream_profile_name():
    """Resolve Dispatcharr's GLOBAL default stream profile's name ONCE per
    report run (I3). 1438 of 1440 channels on the real box carry
    stream_profile=NULL, and Dispatcharr resolves that NULL to the global
    default AT PLAY TIME (core.models.CoreSettings.get_default_stream_profile_id(),
    backed by stream_settings.default_stream_profile) -- NOT to an
    unconditional "always proxying" answer. Hardcoding the old behavior
    silently breaks the moment an operator points the global default at
    Redirect: the whole lineup stops writing live:channel:* keys, but every
    NULL-profile channel would still read as proxying, so `unobservable`
    stays 0 and the ">90% unobservable" gate can never fire.

    Read-only; function-local import (Django is not ready at module import
    time). Fails SAFE (returns None) on any error -- a resolution failure
    must never manufacture a specific-but-wrong verdict; the caller
    (_is_proxying) treats None as "assume proxying", the old behavior.
    """
    try:
        from core.models import CoreSettings  # Dispatcharr runtime only

        profile_id = CoreSettings.get_default_stream_profile_id()
        if not profile_id:
            return None
        from apps.channels.models import StreamProfile  # Dispatcharr runtime only

        profile = StreamProfile.objects.only("name").get(id=profile_id)
        return (getattr(profile, "name", "") or "").strip().lower()
    except Exception:
        return None


def _default_stream_profile_is_redirect():
    """Structural verdict for the GLOBAL default profile: True/False, or None
    when it cannot be resolved (caller fails SAFE and assumes proxying).

    Prefers CoreSettings.is_default_stream_profile_redirect(), the same test
    Dispatcharr's proxy uses at play time (live_proxy/views.py), because the
    NAME heuristic is wrong in both directions: an unlocked profile merely
    NAMED like a redirect is proxied by Dispatcharr and does write
    live:channel:* keys, while nothing but the locked built-in named exactly
    'Redirect' ever redirects. Falls back to the name heuristic only on a
    Dispatcharr old enough to lack the classmethod."""
    try:
        from core.models import CoreSettings  # Dispatcharr runtime only

        structural = getattr(CoreSettings, "is_default_stream_profile_redirect",
                             None)
        if callable(structural):
            return bool(structural())
        name = _default_stream_profile_name()
        if name is None:
            return None
        return name in _NON_PROXYING_NAMES
    except Exception:
        return None


def _is_proxying(channel, default_is_redirect=None):
    """A non-proxying (Redirect) profile never writes live:channel:* keys, so the
    channel is invisible to the collector.

    `default_is_redirect` (I3) is the resolved GLOBAL default profile's
    structural verdict, or None if that resolution itself failed. A NULL
    `channel.stream_profile` is NOT an unconditional "always proxying"
    answer -- Dispatcharr resolves NULL to this global default at play time,
    so a NULL-profile channel must inherit the SAME verdict as that default,
    not a hardcoded True. If the default could not be resolved at all
    (default_is_redirect is None), fail SAFE and assume proxying -- the old
    behavior -- rather than guess.

    An explicit profile is asked its OWN structural verdict first: Dispatcharr
    decides redirect-versus-proxy via StreamProfile.is_redirect() (locked AND
    named exactly 'Redirect'), so a cloned 'Redirect (302)' profile is proxied
    and observable. The name heuristic remains only as the fallback for a
    profile object without the method."""
    profile = getattr(channel, "stream_profile", None)
    if profile is None:
        if default_is_redirect is None:
            return True
        return not default_is_redirect
    structural = getattr(profile, "is_redirect", None)
    if callable(structural):
        try:
            return not structural()
        except Exception:
            pass
    name = (getattr(profile, "name", "") or "").strip().lower()
    return name not in _NON_PROXYING_NAMES
