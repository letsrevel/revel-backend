"""Shared helpers for the split ``test_recurring_events_*.py`` test modules.

These helpers were extracted from the original monolithic
``test_recurring_events.py`` to keep each split file under the project's
1000-line limit while still letting all sibling files share the same
fixtures and payload builders. Names start with an underscore so pytest
does not try to collect this module as a test file.
"""

import typing as t
import uuid
from datetime import datetime, timedelta

from django.utils import timezone

from events.models import (
    Event,
    EventSeries,
    Organization,
    RecurrenceRule,
)


def _create_recurring_event_payload(start: t.Any = None) -> dict[str, t.Any]:
    """Build a valid payload for the create-recurring-event endpoint."""
    if start is None:
        start_dt = timezone.now() + timedelta(days=1)
    else:
        start_dt = start
    end_dt = start_dt + timedelta(hours=2)

    return {
        "event": {
            "name": "Weekly Standup",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "event_type": "public",
            "visibility": "public",
        },
        "series_name": "Weekly Standup Series",
        "series_description": "Our weekly standup meeting",
        "recurrence": {
            "frequency": "weekly",
            "interval": 1,
            "weekdays": [0],
            "dtstart": start_dt.isoformat(),
            "timezone": "UTC",
        },
        "auto_publish": False,
        "generation_window_weeks": 4,
    }


def _make_series_with_rule(
    organization: Organization,
    *,
    dtstart: datetime | None = None,
    name_suffix: str = "",
) -> EventSeries:
    """Create an EventSeries with recurrence rule, template event, and correct FK links.

    Ensures the template event's event_series FK points to the series that owns it,
    so that duplicate_event() copies the correct FK to materialized events.

    Each call gets a unique series name (via a short uuid hex suffix) so
    tests that build multiple series in the same organization don't trip
    the ``(organization, name)`` unique constraint. Using a uuid rather
    than a module-level counter keeps the helper stateless and parallel-safe.
    """
    if dtstart is None:
        dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
    uniq = uuid.uuid4().hex[:8]
    # Use a daily rule so occurrences exist regardless of which weekday
    # ``dtstart`` falls on (callers that pass a future dtstart may land on any
    # day of the week).
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.DAILY,
        interval=1,
        dtstart=dtstart,
    )
    series = EventSeries.objects.create(
        organization=organization,
        name=f"Test Recurring Series {uniq}{name_suffix}",
        recurrence_rule=rule,
        is_active=True,
        auto_publish=False,
        generation_window_weeks=4,
    )
    template_event = Event.objects.create(
        organization=organization,
        event_series=series,
        name=f"Controller Template {uniq}",
        start=dtstart,
        end=dtstart + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
    )
    series.template_event = template_event
    series.save(update_fields=["template_event"])
    return series


def _make_series_with_future_dtstart(organization: Organization) -> EventSeries:
    """Variant of ``_make_series_with_rule`` whose dtstart is in the near future.

    Used by tests that can't freeze time (JWT auth would fail) but still need
    ``generate_series_events`` to materialize occurrences that satisfy
    ``start__gte=timezone.now()`` downstream.
    """
    dtstart = timezone.now() + timedelta(days=1)
    # Snap to the next weekday so the weekly rule has a stable anchor.
    return _make_series_with_rule(organization, dtstart=dtstart, name_suffix=" future")
