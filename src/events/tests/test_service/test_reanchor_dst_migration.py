"""Tests for the 0087 re-anchor data migration.

The migration realigns future, unmodified, non-cancelled occurrences of non-UTC
recurring series to the DST-correct wall-clock instant. We exercise the
migration function directly with Django's live app registry (no extra
migration-testing dependency) and assert it corrects only the rows it should.
"""

import importlib
import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.apps import apps as django_apps
from freezegun import freeze_time

from events.models import Event, EventSeries, Organization, RecurrenceRule

pytestmark = pytest.mark.django_db

VIENNA = ZoneInfo("Europe/Vienna")
# The migration module name starts with a digit, so it can't be a normal import.
_migration = importlib.import_module("events.migrations.0087_reanchor_non_utc_series_occurrences")
reanchor = _migration.reanchor_non_utc_occurrences


def _make_series(organization: Organization, tz: str) -> EventSeries:
    """Create an active series anchored to Mondays 10:00 in ``tz`` (pre-DST)."""
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],
        dtstart=datetime(2026, 3, 23, 10, 0, tzinfo=ZoneInfo(tz)),
        timezone=tz,
    )
    series = EventSeries.objects.create(
        organization=organization,
        name=f"Series {tz}",
        slug=f"series-{tz.lower().replace('/', '-')}",
        recurrence_rule=rule,
    )
    return series


def _make_occurrence(
    series: EventSeries,
    organization: Organization,
    *,
    start: datetime,
    slug: str,
    is_modified: bool = False,
    status: str = Event.EventStatus.OPEN,
) -> Event:
    return Event.objects.create(
        organization=organization,
        event_series=series,
        name="Occurrence",
        slug=slug,
        start=start,
        end=start + timedelta(hours=2),
        status=status,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=False,
        is_modified=is_modified,
        requires_ticket=False,
    )


@freeze_time("2026-03-01 00:00:00")
@patch("notifications.service.notification_helpers.notify_series_events_generated")
def test_migration_reanchors_future_non_utc_occurrence(
    mock_notify: t.Any,
    organization: Organization,
) -> None:
    """A drifted post-DST occurrence is corrected to 10:00 Vienna (08:00 UTC)."""
    series = _make_series(organization, "Europe/Vienna")
    # Old UTC-anchored behavior put this post-DST Monday at 09:00 UTC (11:00 CEST).
    drifted = _make_occurrence(
        series,
        organization,
        start=datetime(2026, 4, 6, 9, 0, tzinfo=dt_timezone.utc),
        slug="drifted",
    )

    reanchor(django_apps, None)

    drifted.refresh_from_db()
    assert drifted.start == datetime(2026, 4, 6, 8, 0, tzinfo=dt_timezone.utc)
    local = drifted.start.astimezone(VIENNA)
    assert (local.hour, local.minute) == (10, 0)
    # Duration preserved: end shifts by the same delta.
    assert drifted.end == datetime(2026, 4, 6, 10, 0, tzinfo=dt_timezone.utc)


@freeze_time("2026-03-01 00:00:00")
@patch("notifications.service.notification_helpers.notify_series_events_generated")
def test_migration_leaves_utc_modified_cancelled_and_past_untouched(
    mock_notify: t.Any,
    organization: Organization,
) -> None:
    """UTC, modified, cancelled, and past occurrences are not re-anchored."""
    # UTC series — no-op zone.
    utc_series = _make_series(organization, "UTC")
    utc_occ = _make_occurrence(
        utc_series,
        organization,
        start=datetime(2026, 4, 6, 9, 0, tzinfo=dt_timezone.utc),
        slug="utc-occ",
    )

    vienna_series = _make_series(organization, "Europe/Vienna")
    modified = _make_occurrence(
        vienna_series,
        organization,
        start=datetime(2026, 4, 13, 9, 0, tzinfo=dt_timezone.utc),
        slug="modified",
        is_modified=True,
    )
    cancelled = _make_occurrence(
        vienna_series,
        organization,
        start=datetime(2026, 4, 20, 9, 0, tzinfo=dt_timezone.utc),
        slug="cancelled",
        status=Event.EventStatus.CANCELLED,
    )
    # Past occurrence (before frozen "now") — out of scope, never reschedule history.
    past = _make_occurrence(
        vienna_series,
        organization,
        start=datetime(2026, 2, 23, 9, 0, tzinfo=dt_timezone.utc),
        slug="past",
    )

    originals = {e.pk: e.start for e in (utc_occ, modified, cancelled, past)}

    reanchor(django_apps, None)

    for event in (utc_occ, modified, cancelled, past):
        event.refresh_from_db()
        assert event.start == originals[event.pk]
