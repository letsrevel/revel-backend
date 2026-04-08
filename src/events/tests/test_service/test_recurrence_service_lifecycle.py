"""Tests for the recurrence service: cancel, pause/resume, update_recurrence.

Split from the original ``test_recurrence_service.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover materialization
(materialize/generate) and template propagation. Shared fixtures live in
``conftest.py``.
"""

import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries, RecurrenceRule
from events.service import recurrence_service

pytestmark = pytest.mark.django_db


class TestCancelOccurrence:
    """Tests for the cancel_occurrence function."""

    def test_adds_date_to_exdates(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that cancelling adds the UTC-normalized date string to exdates."""
        # Arrange
        occurrence_date = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        assert active_series.exdates == []

        # Act
        recurrence_service.cancel_occurrence(active_series, occurrence_date)

        # Assert — stored in UTC so the same instant sent in different tzs collapses to one entry.
        active_series.refresh_from_db()
        assert len(active_series.exdates) == 1
        expected = occurrence_date.astimezone(dt_timezone.utc).isoformat()
        assert expected in active_series.exdates

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_cancels_materialized_event(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that a materialized event gets CANCELLED status."""
        # Arrange - generate events first
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) > 0
        target_event = created[0]
        target_date = target_event.start

        # Act
        recurrence_service.cancel_occurrence(active_series, target_date)

        # Assert
        target_event.refresh_from_db()
        assert target_event.status == Event.EventStatus.CANCELLED

    def test_no_error_when_no_materialized_event(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that cancelling a non-materialized date just adds exdate without error."""
        # Arrange
        occurrence_date = timezone.make_aware(datetime(2026, 5, 1, 10, 0))

        # Act - should not raise
        recurrence_service.cancel_occurrence(active_series, occurrence_date)

        # Assert
        active_series.refresh_from_db()
        expected = occurrence_date.astimezone(dt_timezone.utc).isoformat()
        assert expected in active_series.exdates

    def test_no_duplicate_exdate_on_repeated_calls(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that calling cancel_occurrence twice for the same date doesn't duplicate."""
        # Arrange
        occurrence_date = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        recurrence_service.cancel_occurrence(active_series, occurrence_date)
        recurrence_service.cancel_occurrence(active_series, occurrence_date)

        # Assert
        active_series.refresh_from_db()
        expected = occurrence_date.astimezone(dt_timezone.utc).isoformat()
        assert active_series.exdates.count(expected) == 1


class TestCancelOccurrenceNormalization:
    """``cancel_occurrence`` must normalize datetimes to UTC to prevent duplicate exdates."""

    def test_same_instant_different_tz_not_duplicated(
        self,
        active_series: EventSeries,
    ) -> None:
        """Sending the same instant in Europe/Rome and UTC must produce exactly one exdate."""
        # Arrange — Apr 13, 2026 10:00 UTC == Apr 13, 2026 12:00 Europe/Rome (DST).
        utc_instant = datetime(2026, 4, 13, 10, 0, tzinfo=dt_timezone.utc)
        rome_offset = timedelta(hours=2)
        rome_equiv = datetime(2026, 4, 13, 12, 0, tzinfo=dt_timezone(rome_offset))
        assert utc_instant == rome_equiv  # sanity: same instant

        # Act
        recurrence_service.cancel_occurrence(active_series, utc_instant)
        recurrence_service.cancel_occurrence(active_series, rome_equiv)

        # Assert
        active_series.refresh_from_db()
        assert len(active_series.exdates) == 1


class TestPauseResumeSeries:
    """Tests for pause_series and resume_series functions."""

    def test_pause_series_sets_inactive(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that pause_series sets is_active to False."""
        # Arrange
        assert active_series.is_active is True

        # Act
        recurrence_service.pause_series(active_series)

        # Assert
        active_series.refresh_from_db()
        assert active_series.is_active is False

    def test_resume_series_sets_active(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that resume_series sets is_active to True."""
        # Arrange
        active_series.is_active = False
        active_series.save(update_fields=["is_active"])

        # Act
        recurrence_service.resume_series(active_series)

        # Assert
        active_series.refresh_from_db()
        assert active_series.is_active is True


class TestUpdateSeriesRecurrenceResets:
    """Rule/window changes must reset ``last_generated_until`` so the new cadence takes effect."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_rule_change_resets_last_generated_until(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Switching weekly → daily must make the next generation produce daily events immediately."""
        # Arrange — generate weekly first so last_generated_until is populated.
        recurrence_service.generate_series_events(active_series)
        first_run_series = EventSeries.objects.get(pk=active_series.pk)
        assert first_run_series.last_generated_until is not None

        # Act — swap to a daily rule. This should reset the cursor.
        recurrence_service.update_series_recurrence(
            active_series,
            recurrence_data={"frequency": RecurrenceRule.Frequency.DAILY, "weekdays": []},
        )
        # Re-fetch from the DB so mypy sees a fresh instance (refresh_from_db mutates
        # in place but mypy can't track that, so the previous narrowing would persist).
        post_update_series = EventSeries.objects.get(pk=active_series.pk)

        # Assert — cursor cleared
        assert post_update_series.last_generated_until is None

        # And the next generation should backfill daily events, not stall
        created = recurrence_service.generate_series_events(post_update_series)
        # 4 weeks daily from Monday 2026-04-06 = 28 daily occurrences (dtstart-1s, horizon).
        # Only the new ones (not the existing weekly Mondays) are returned: 28 - 4 = 24.
        assert len(created) >= 20

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_generation_window_decrease_resets_cursor(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Decreasing generation_window_weeks must not permanently stall generation."""
        # Arrange — start with an 8-week window and generate.
        active_series.generation_window_weeks = 8
        active_series.save(update_fields=["generation_window_weeks"])
        recurrence_service.generate_series_events(active_series)
        active_series.refresh_from_db()
        horizon_after_first_run = active_series.last_generated_until
        assert horizon_after_first_run is not None

        # Act — decrease window to 4 weeks (below the current horizon).
        recurrence_service.update_series_recurrence(
            active_series,
            generation_window_weeks=4,
        )
        active_series.refresh_from_db()

        # Assert — cursor is cleared so the next generate_series_events doesn't stall.
        assert active_series.last_generated_until is None

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_auto_publish_only_does_not_reset_cursor(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Updating only ``auto_publish`` is not schedule-affecting, so the cursor stays."""
        # Arrange
        recurrence_service.generate_series_events(active_series)
        active_series.refresh_from_db()
        before = active_series.last_generated_until
        assert before is not None

        # Act
        recurrence_service.update_series_recurrence(active_series, auto_publish=True)
        active_series.refresh_from_db()

        # Assert — cursor unchanged because nothing schedule-affecting moved.
        assert active_series.last_generated_until == before
        assert active_series.auto_publish is True
