"""Bounds on recurrence generation and drift inspection."""

from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from events.models import Event, EventSeries
from events.service import recurrence_service

pytestmark = pytest.mark.django_db


@freeze_time("2026-04-06 00:00:00")
def test_generation_materializes_at_most_the_per_run_cap(
    active_series: EventSeries, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run stops at the cap and the next run resumes from the last materialized occurrence."""
    monkeypatch.setattr(recurrence_service, "MAX_OCCURRENCES_PER_RUN", 2)

    first = recurrence_service.generate_series_events(active_series)
    second = recurrence_service.generate_series_events(active_series)

    assert len(first) == 2
    assert second
    assert {e.start for e in first}.isdisjoint({e.start for e in second})


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
