"""Tests for the recurrence service: materialization and rolling-window generation.

Split from the original ``test_recurrence_service.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover lifecycle
operations (cancel/pause/resume/update_recurrence) and template propagation.
Shared fixtures (``weekly_rule``, ``template_event``, ``active_series``,
``active_series_with_tier``) live in ``conftest.py``.
"""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries, RecurrenceRule
from events.models.mixins import ResourceVisibility
from events.service import recurrence_service

pytestmark = pytest.mark.django_db


class TestMaterializeOccurrence:
    """Tests for the materialize_occurrence function."""

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_creates_event_with_correct_name_and_start(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that materialized event has the template's name and the given start date."""
        # Arrange
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=1)

        # Assert
        assert event.name == "Weekly Meetup"
        assert event.start == dt
        assert event.occurrence_index == 1

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_creates_event_with_is_template_false(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that the materialized event is NOT a template."""
        # Arrange
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)

        # Assert
        assert event.is_template is False

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_creates_event_with_correct_event_series_fk(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that the materialized event belongs to the correct event series."""
        # Arrange
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)

        # Assert
        assert event.event_series_id == active_series.id

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_auto_publish_false_creates_draft(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that auto_publish=False creates the event in DRAFT status."""
        # Arrange
        assert active_series.auto_publish is False
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)

        # Assert
        assert event.status == Event.EventStatus.DRAFT

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_auto_publish_true_creates_open(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that auto_publish=True creates the event in OPEN status."""
        # Arrange
        active_series.auto_publish = True
        active_series.save(update_fields=["auto_publish"])
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)

        # Assert
        assert event.status == Event.EventStatus.OPEN

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_ticket_tiers_copied_from_template(
        self,
        mock_notify: t.Any,
        active_series_with_tier: EventSeries,
    ) -> None:
        """Test that ticket tiers from the template are duplicated onto the new event."""
        # Arrange
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series_with_tier, dt, index=0)

        # Assert
        tiers = list(event.ticket_tiers.all())
        assert len(tiers) >= 1
        tier_names = [t.name for t in tiers]
        assert "General Admission" in tier_names
        ga_tier = next(t for t in tiers if t.name == "General Admission")
        assert ga_tier.quantity_sold == 0  # Reset

    def test_raises_value_error_when_no_template(
        self,
        event_series: EventSeries,
    ) -> None:
        """Test that materializing without a template raises ValueError."""
        # Arrange
        assert event_series.template_event is None
        dt = timezone.now()

        # Act & Assert
        with pytest.raises(ValueError, match="no template event"):
            recurrence_service.materialize_occurrence(event_series, dt, index=0)

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_propagatable_fields_survive_materialization(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Regression test: template fields listed in ``PROPAGATABLE_FIELDS`` must
        be carried over to materialized occurrences.

        This guards against the class of bug where ``duplicate_event`` silently
        omits a propagatable field, leaving occurrences diverged from the
        template from the moment of creation. Every field in
        ``PROPAGATABLE_FIELDS`` should either be asserted here or have a dedicated
        test.
        """
        # Arrange — set non-default values on the template for each propagatable field
        # we care about. These are the fields most recently found missing in review.
        template = active_series.template_event
        assert template is not None
        template.max_tickets_per_user = 5
        template.public_pronoun_distribution = True
        template.requires_full_profile = True
        template.address_visibility = ResourceVisibility.MEMBERS_ONLY
        template.accept_invitation_requests = True
        template.invitation_message = "Welcome to the weekly meetup"
        template.max_attendees = 42
        template.can_attend_without_login = False
        template.save()

        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))

        # Act
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)

        # Assert — every field survived the duplicate.
        assert event.max_tickets_per_user == 5
        assert event.public_pronoun_distribution is True
        assert event.requires_full_profile is True
        assert event.address_visibility == ResourceVisibility.MEMBERS_ONLY
        assert event.accept_invitation_requests is True
        assert event.invitation_message == "Welcome to the weekly meetup"
        assert event.max_attendees == 42
        assert event.can_attend_without_login is False

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_materialized_event_is_not_modified(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """A freshly materialized occurrence must start with ``is_modified=False``
        so propagation reaches it on subsequent template edits."""
        dt = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        event = recurrence_service.materialize_occurrence(active_series, dt, index=0)
        assert event.is_modified is False


class TestGenerateSeriesEvents:
    """Tests for the generate_series_events function."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_weekly_rule_generates_expected_count(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that a weekly Monday rule over a 4-week window generates the expected Mondays.

        Boundary note: ``dtstart`` is built via ``timezone.make_aware`` which uses
        Django's TIME_ZONE (Europe/Vienna, +02:00 in April), so wall-clock 10:00
        equals 08:00 UTC. ``timezone.now()`` under ``freeze_time`` returns the
        frozen value as UTC (10:00 UTC). The 4-week horizon is therefore
        2026-05-04 10:00 UTC, and the 5th Monday at 2026-05-04 08:00 UTC fits
        inside the window — yielding 5 occurrences in total.
        """
        # Act
        created = recurrence_service.generate_series_events(active_series)

        # Assert — Mondays at 08:00 UTC (10:00 Vienna): Apr 6, Apr 13, Apr 20, Apr 27, May 4.
        starts = sorted(e.start for e in created)
        assert starts == [
            timezone.make_aware(datetime(2026, 4, 6, 10, 0)),
            timezone.make_aware(datetime(2026, 4, 13, 10, 0)),
            timezone.make_aware(datetime(2026, 4, 20, 10, 0)),
            timezone.make_aware(datetime(2026, 4, 27, 10, 0)),
            timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        ]
        for event in created:
            assert event.is_template is False
            assert event.event_series_id == active_series.id

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_skips_exdates(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that dates in exdates are skipped during generation."""
        # Arrange - exclude April 13 (using same timezone as dtstart)
        april_13 = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        active_series.exdates = [april_13.isoformat()]
        active_series.save(update_fields=["exdates"])

        # Act
        created = recurrence_service.generate_series_events(active_series)

        # Assert
        created_starts = [e.start for e in created]
        assert april_13 not in created_starts

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_idempotent_skips_existing_occurrences(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that running generate twice returns empty list the second time (idempotency)."""
        # Act
        first_run = recurrence_service.generate_series_events(active_series)
        second_run = recurrence_service.generate_series_events(active_series)

        # Assert
        assert len(first_run) > 0
        assert len(second_run) == 0

    def test_returns_empty_when_series_inactive(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that an inactive series returns empty list."""
        # Arrange
        active_series.is_active = False
        active_series.save(update_fields=["is_active"])

        # Act
        result = recurrence_service.generate_series_events(active_series)

        # Assert
        assert result == []

    def test_returns_empty_when_no_recurrence_rule(
        self,
        event_series: EventSeries,
        template_event: Event,
    ) -> None:
        """Test that a series without a recurrence rule returns empty list."""
        # Arrange
        event_series.template_event = template_event
        event_series.is_active = True
        event_series.save()

        # Act
        result = recurrence_service.generate_series_events(event_series)

        # Assert
        assert result == []

    def test_returns_empty_when_no_template_event(
        self,
        event_series: EventSeries,
        weekly_rule: RecurrenceRule,
    ) -> None:
        """Test that a series without a template event returns empty list."""
        # Arrange
        event_series.recurrence_rule = weekly_rule
        event_series.is_active = True
        event_series.save()

        # Act
        result = recurrence_service.generate_series_events(event_series)

        # Assert
        assert result == []

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_updates_last_generated_until(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that last_generated_until is set after generation."""
        # Arrange
        assert active_series.last_generated_until is None

        # Act
        recurrence_service.generate_series_events(active_series)

        # Assert
        active_series.refresh_from_db()
        assert active_series.last_generated_until is not None

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_with_until_override(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that until_override limits the generation horizon."""
        # Arrange - only generate up to Apr 20 23:59 instead of the full 4-week window
        override = timezone.make_aware(datetime(2026, 4, 20, 23, 59))

        # Act
        created = recurrence_service.generate_series_events(active_series, until_override=override)

        # Assert — Mondays in (dtstart-1s, override) exclusive: Apr 6, Apr 13, Apr 20
        starts = sorted(e.start for e in created)
        assert starts == [
            timezone.make_aware(datetime(2026, 4, 6, 10, 0)),
            timezone.make_aware(datetime(2026, 4, 13, 10, 0)),
            timezone.make_aware(datetime(2026, 4, 20, 10, 0)),
        ]
        for event in created:
            assert event.start <= override

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_notifies_after_generation(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that notify_series_events_generated is called when events are created."""
        # Act
        created = recurrence_service.generate_series_events(active_series)

        # Assert
        assert len(created) > 0
        mock_notify.assert_called_once_with(active_series, created)

    @freeze_time("2026-04-06 10:00:00")
    def test_no_notification_when_nothing_generated(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that no notification is sent when no events are generated."""
        # Arrange
        active_series.is_active = False
        active_series.save(update_fields=["is_active"])

        # Act
        with patch("notifications.service.notification_helpers.notify_series_events_generated") as mock_notify:
            recurrence_service.generate_series_events(active_series)

        # Assert
        mock_notify.assert_not_called()


class TestGenerateWindowDecreaseDefense:
    """``generate_series_events`` must self-correct when start_from exceeds horizon."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_past_horizon_cursor_does_not_stall(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """If a legacy series has a future last_generated_until beyond the new horizon, don't loop forever."""
        # Arrange — fabricate a future cursor and a small window.
        active_series.generation_window_weeks = 4
        active_series.last_generated_until = timezone.now() + timedelta(weeks=26)
        active_series.save(update_fields=["generation_window_weeks", "last_generated_until"])

        # Act — must not raise and must complete cleanly.
        created = recurrence_service.generate_series_events(active_series)

        # Assert — no events generated (start_from clamps to horizon), horizon updated.
        assert created == []
        active_series.refresh_from_db()
        # Horizon moved back to the real one (now + 4 weeks), not the stale future cursor.
        assert active_series.last_generated_until == timezone.now() + timedelta(weeks=4)


class TestGenerateSeriesMonotonicIndex:
    """``occurrence_index`` must remain monotonic across rolling-window runs."""

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_indices_continue_after_window_advance(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """A second generation run should continue the index sequence from where the first stopped.

        The ``active_series`` fixture uses a 4-week window. Advancing time
        pushes the rolling horizon past the already-generated range so new
        Mondays are materialized.
        """
        # Arrange — first run at t0 (Mon 2026-04-06).
        with freeze_time("2026-04-06 10:00:00"):
            first = recurrence_service.generate_series_events(active_series)
        assert first, "first run must produce events"
        first_max = max(e.occurrence_index for e in first if e.occurrence_index is not None)

        # Act — advance time so the horizon (now + 4 weeks) moves past the old cursor.
        with freeze_time("2026-05-11 11:00:00"):
            second = recurrence_service.generate_series_events(active_series)

        # Assert — new events continue the sequence strictly above first_max.
        assert second, "second run must produce new events"
        for event in second:
            assert event.occurrence_index is not None
            assert event.occurrence_index > first_max
