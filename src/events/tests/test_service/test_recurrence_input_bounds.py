"""Bounds on recurrence generation and drift inspection."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries
from events.service import recurrence_service
from events.utils.recurrence_validators import MAX_DTSTART_LOOKBACK_DAYS

pytestmark = pytest.mark.django_db


@freeze_time("2026-04-06 00:00:00")
def test_generation_floors_old_dtstart_and_still_reaches_horizon(active_series: EventSeries) -> None:
    """An ancient dtstart with a reset cursor never materializes below the floor, yet fills the window."""
    rule = active_series.recurrence_rule
    assert rule is not None
    rule.dtstart = timezone.now() - timedelta(days=3 * 365)
    rule.save()
    active_series.last_generated_until = None
    active_series.save()

    created = recurrence_service.generate_series_events(active_series)

    floor = timezone.now() - timedelta(days=MAX_DTSTART_LOOKBACK_DAYS)
    assert created
    assert min(e.start for e in created) >= floor
    assert max(e.start for e in created) > timezone.now() + timedelta(weeks=2)


@freeze_time("2026-04-06 00:00:00")
def test_drift_ignores_events_beyond_generation_ceiling(active_series: EventSeries) -> None:
    """An off-cadence occurrence far past the generation ceiling is not enumerated against the rule."""
    start = datetime.fromisoformat("2200-01-01T11:00:00+00:00")
    Event.objects.create(
        organization=active_series.organization,
        event_series=active_series,
        name="Far future",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=False,
    )

    assert recurrence_service.detect_cadence_drift(active_series) == []
