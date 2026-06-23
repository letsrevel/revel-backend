"""Re-anchor future occurrences of non-UTC recurring series to DST-correct times.

Before timezone-aware occurrence generation landed, ``RecurrenceRule.to_rrule()``
anchored occurrences to the UTC ``dtstart`` and ignored the rule's ``timezone``,
so a "Mondays 10:00 Europe/Vienna" series drifted to 11:00 Vienna after the
spring DST switch. ``to_rrule()`` now localizes the anchor into the named zone.

This data migration realigns already-materialized occurrences so they match the
corrected cadence (and so cadence-drift detection doesn't flag them). It only
touches **future, unmodified, non-cancelled** occurrences of series whose rule
uses a **non-UTC** timezone — the default ``UTC`` is a no-op, and modified
occurrences were deliberately shifted off-cadence by an organiser. Each shift is
at most the DST offset (≤ 1 hour); ``start`` and ``end`` move together so the
event's duration is preserved.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import migrations
from django.utils import timezone

logger = logging.getLogger(__name__)


def reanchor_non_utc_occurrences(apps, schema_editor):
    """Shift future non-UTC occurrences to the DST-correct wall-clock instant."""
    Event = apps.get_model("events", "Event")

    now = timezone.now()
    qs = (
        Event.objects.filter(
            is_template=False,
            is_modified=False,
            start__gte=now,
            event_series__recurrence_rule__isnull=False,
        )
        .exclude(event_series__recurrence_rule__timezone="UTC")
        .exclude(status="cancelled")
        .select_related("event_series__recurrence_rule")
    )

    updated = 0
    for event in qs:
        rule = event.event_series.recurrence_rule
        try:
            tz = ZoneInfo(rule.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # Defensive: a row with an unparseable zone shouldn't block the
            # whole migration. Validation rejects these on save, so this is
            # only a guard against legacy/hand-edited data.
            continue

        anchor_local = rule.dtstart.astimezone(tz)
        old_local = event.start.astimezone(tz)
        # Recompute the occurrence on its *local* calendar date with the anchor's
        # wall-clock time-of-day. A DST drift that pushed the old start across
        # local midnight (only possible for anchors within ~1h of midnight) would
        # land on the wrong day, producing a delta larger than the DST offset; the
        # >1h guard below skips those rather than rewrite them to the wrong day.
        new_start = datetime(
            old_local.year,
            old_local.month,
            old_local.day,
            anchor_local.hour,
            anchor_local.minute,
            anchor_local.second,
            anchor_local.microsecond,
            tzinfo=tz,
        )
        if new_start == event.start:
            continue

        delta = new_start - event.start
        if abs(delta) > timedelta(hours=1):
            # A correct DST re-anchor shifts by at most the DST offset (≤1h).
            # A larger delta means the local-date recombination crossed a day
            # boundary (near-midnight anchor edge); skip to avoid a wrong-day move.
            continue
        event.start = new_start
        event.end = event.end + delta
        # Historical-model save() runs no signals/full_clean, so no notifications
        # fire — organisers communicate the corrected time out-of-band if needed.
        event.save(update_fields=["start", "end"])
        updated += 1

    logger.info("reanchor_non_utc_occurrences_complete", extra={"updated": updated})


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0086_event_is_open_ended"),
    ]

    operations = [
        migrations.RunPython(
            reanchor_non_utc_occurrences,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
    ]
