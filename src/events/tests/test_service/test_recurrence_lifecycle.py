"""Full lifecycle integration test for recurring event series.

Covers the happy-path flow that would catch interactions between the
individually-tested pieces in ``test_recurrence_service.py``:

    create series -> generate (beat) -> edit template -> propagate -> cancel.

The individual pieces have dedicated unit tests; this file is deliberately
narrow and asserts on end-to-end business state (not on internal helpers).
"""

import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries, Organization, RecurrenceRule
from events.service import recurrence_service
from events.service.recurrence_service import PropagateScope

pytestmark = pytest.mark.django_db


class TestFullRecurringEventLifecycle:
    """End-to-end lifecycle: create → generate → edit → propagate → cancel."""

    @freeze_time("2026-04-06 00:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_full_lifecycle(
        self,
        mock_notify: t.Any,
        organization: Organization,
    ) -> None:
        """Full lifecycle smoke test.

        Ensures the individually-tested pieces compose correctly. Uses the
        public service API exclusively (no internal helpers) so it would catch
        regressions in cross-step interactions: e.g. if ``duplicate_event``
        stopped copying a field that ``propagate_template_changes`` then tries
        to re-apply, the propagation count would be wrong.

        Frozen time is set to Apr 6 00:00 UTC (before the first Monday at
        08:00 UTC / 10:00 Vienna) so every materialized occurrence is in the
        future relative to ``timezone.now()`` — otherwise
        ``propagate_template_changes`` would filter out the first occurrence
        via its ``start__gte=now()`` guard and skew the assertion counts.
        """
        dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))  # Monday, 10:00 Vienna = 08:00 UTC

        # 1. Create a recurring series atomically (rule + series + template +
        #    initial generation within a 5-week rolling window).
        series = recurrence_service.create_recurring_event_series(
            organization=organization,
            recurrence_data={
                "frequency": RecurrenceRule.Frequency.WEEKLY,
                "interval": 1,
                "weekdays": [0],
                "dtstart": dtstart,
            },
            series_name="Weekly Meetup",
            series_description="Every Monday",
            auto_publish=False,
            generation_window_weeks=5,
            event_data={
                "name": "Weekly Meetup",
                "start": dtstart,
                "end": dtstart + timedelta(hours=2),
                "status": Event.EventStatus.DRAFT,
                "visibility": Event.Visibility.PUBLIC,
                "event_type": Event.EventType.PUBLIC,
                "requires_ticket": True,
                "description": "Original description",
                "max_attendees": 50,
            },
        )

        # Assert initial materialization produced a handful of future Mondays.
        initial_events = list(series.events.filter(is_template=False).order_by("start"))
        assert len(initial_events) >= 4, "expected at least 4 Mondays in a 5-week window"
        for event in initial_events:
            assert event.is_template is False
            assert event.is_modified is False
            assert event.description == "Original description"
            assert event.max_attendees == 50
            # Every occurrence must be in the future relative to frozen now
            # so propagation's start__gte=now() filter catches them all.
            assert event.start > timezone.now()

        # 2. Mark one occurrence as manually modified so we can verify scope
        #    filtering in step 3.
        manually_edited = initial_events[1]
        manually_edited.is_modified = True
        manually_edited.description = "Hand-edited"
        manually_edited.save(update_fields=["is_modified", "description"])

        # 3. Edit the template and propagate only to FUTURE_UNMODIFIED.
        from events.schema.recurring_event import TemplateEditSchema  # noqa: PLC0415

        edit_payload = TemplateEditSchema(description="New description", max_attendees=100)
        updated_series = recurrence_service.update_template(
            series=series,
            payload=edit_payload,
            scope=PropagateScope.FUTURE_UNMODIFIED,
        )
        assert updated_series.template_event is not None
        assert updated_series.template_event.description == "New description"
        assert updated_series.template_event.max_attendees == 100

        # 4. Assert propagation reached all future unmodified events but NOT the
        #    manually-edited one (is_modified=True protects it).
        for event in initial_events:
            event.refresh_from_db()
        manually_edited.refresh_from_db()
        assert manually_edited.description == "Hand-edited"
        assert manually_edited.max_attendees == 50
        unmodified_future = [e for e in initial_events if not e.is_modified]
        assert len(unmodified_future) == len(initial_events) - 1
        for event in unmodified_future:
            assert event.description == "New description"
            assert event.max_attendees == 100

        # 5. Cancel an occurrence. The exdate is stored UTC-normalized and the
        #    materialized event flips to CANCELLED.
        target = initial_events[0]
        recurrence_service.cancel_occurrence(series, target.start)
        target.refresh_from_db()
        series.refresh_from_db()
        assert target.status == Event.EventStatus.CANCELLED
        assert len(series.exdates) == 1
        expected_exdate = target.start.astimezone(dt_timezone.utc).isoformat()
        assert series.exdates[0] == expected_exdate

        # 6. Re-running generation is idempotent — cancelled instants are in
        #    exdates so they are skipped, and existing instants are already in
        #    the DB so they are not duplicated.
        second_run = recurrence_service.generate_series_events(series)
        assert second_run == []

        # 7. Notifications: one per generation batch, not one per event. The
        #    initial create_recurring_event_series call invoked notify once.
        assert mock_notify.call_count == 1

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_pause_then_edit_then_resume_regenerates(
        self,
        mock_notify: t.Any,
        organization: Organization,
    ) -> None:
        """Pausing blocks generation, resuming without rule changes does not
        backfill, but a window increase after resume triggers new materialization.
        """
        dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
        series = recurrence_service.create_recurring_event_series(
            organization=organization,
            recurrence_data={
                "frequency": RecurrenceRule.Frequency.WEEKLY,
                "interval": 1,
                "weekdays": [0],
                "dtstart": dtstart,
            },
            series_name="Paused Meetup",
            series_description=None,
            auto_publish=False,
            generation_window_weeks=2,
            event_data={
                "name": "Paused Meetup",
                "start": dtstart,
                "end": dtstart + timedelta(hours=1),
                "status": Event.EventStatus.DRAFT,
                "visibility": Event.Visibility.PUBLIC,
                "event_type": Event.EventType.PUBLIC,
                "requires_ticket": False,
            },
        )
        initial_count = series.events.filter(is_template=False).count()
        assert initial_count >= 2

        # Pause → generation becomes a no-op even if called.
        recurrence_service.pause_series(series)
        series.refresh_from_db()
        new_events = recurrence_service.generate_series_events(series)
        assert new_events == []

        # Resume → still the same count until the window expands.
        recurrence_service.resume_series(series)
        series.refresh_from_db()
        assert series.events.filter(is_template=False).count() == initial_count

        # Expand the window: cursor resets, so the next generation backfills.
        recurrence_service.update_series_recurrence(series, generation_window_weeks=6)
        series = EventSeries.objects.get(pk=series.pk)
        assert series.last_generated_until is None
        backfill = recurrence_service.generate_series_events(series)
        assert len(backfill) >= 1
        assert series.events.filter(is_template=False).count() > initial_count
