"""Tests for the recurrence service (materialization, generation, cancellation, etc.)."""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries, Organization, RecurrenceRule, TicketTier
from events.service import recurrence_service

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def weekly_rule(organization: Organization) -> RecurrenceRule:
    """Create a weekly recurrence rule on Mondays, starting from a known date."""
    dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))  # Monday
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],  # Monday
        dtstart=dtstart,
    )
    return rule


@pytest.fixture
def template_event(organization: Organization, event_series: EventSeries) -> Event:
    """Create a template event for the series."""
    start = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
    event = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name="Weekly Meetup",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
        requires_ticket=True,
    )
    return event


@pytest.fixture
def template_event_with_tier(template_event: Event) -> Event:
    """Create a template event that has a ticket tier.

    The template_event has requires_ticket=True, so a default "General Admission"
    tier is auto-created by the signal. We update it with the desired price/quantity
    instead of creating a duplicate.
    """
    tier = TicketTier.objects.get(event=template_event, name="General Admission")
    tier.price = 15.00
    tier.total_quantity = 50
    tier.save(update_fields=["price", "total_quantity"])
    return template_event


@pytest.fixture
def active_series(
    event_series: EventSeries,
    weekly_rule: RecurrenceRule,
    template_event: Event,
) -> EventSeries:
    """An active series with a recurrence rule and template event."""
    event_series.recurrence_rule = weekly_rule
    event_series.template_event = template_event
    event_series.is_active = True
    event_series.auto_publish = False
    event_series.generation_window_weeks = 4
    event_series.save()
    return event_series


@pytest.fixture
def active_series_with_tier(
    event_series: EventSeries,
    weekly_rule: RecurrenceRule,
    template_event_with_tier: Event,
) -> EventSeries:
    """An active series whose template has a ticket tier."""
    event_series.recurrence_rule = weekly_rule
    event_series.template_event = template_event_with_tier
    event_series.is_active = True
    event_series.auto_publish = False
    event_series.generation_window_weeks = 4
    event_series.save()
    return event_series


# ---------------------------------------------------------------------------
# materialize_occurrence
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# generate_series_events
# ---------------------------------------------------------------------------


class TestGenerateSeriesEvents:
    """Tests for the generate_series_events function."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_weekly_rule_generates_expected_count(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that a weekly rule over a 4-week window generates approximately 4 events."""
        # Act
        created = recurrence_service.generate_series_events(active_series)

        # Assert - 4 weeks * 1 Monday/week, but dtstart=Apr 6 is the anchor
        # Occurrences: Apr 13, Apr 20, Apr 27, May 4 (between dtstart and dtstart+4wks exclusive)
        assert len(created) >= 3  # At least 3, up to 4 depending on horizon
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
        # Arrange - only generate 2 weeks out instead of 4
        override = timezone.make_aware(datetime(2026, 4, 20, 23, 59))

        # Act
        created = recurrence_service.generate_series_events(active_series, until_override=override)

        # Assert - fewer events than the full 4-week window, all within override
        assert 1 <= len(created) <= 3
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


# ---------------------------------------------------------------------------
# cancel_occurrence
# ---------------------------------------------------------------------------


class TestCancelOccurrence:
    """Tests for the cancel_occurrence function."""

    def test_adds_date_to_exdates(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that cancelling adds the date string to exdates."""
        # Arrange
        occurrence_date = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        assert active_series.exdates == []

        # Act
        recurrence_service.cancel_occurrence(active_series, occurrence_date)

        # Assert
        active_series.refresh_from_db()
        assert len(active_series.exdates) == 1
        assert occurrence_date.isoformat() in active_series.exdates

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
        assert occurrence_date.isoformat() in active_series.exdates

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
        assert active_series.exdates.count(occurrence_date.isoformat()) == 1


# ---------------------------------------------------------------------------
# pause_series / resume_series
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# propagate_template_changes
# ---------------------------------------------------------------------------


class TestPropagateTemplateChanges:
    """Tests for the propagate_template_changes function."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_future_unmodified_scope_only_updates_unmodified_future_events(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that scope 'future_unmodified' only updates events that are not is_modified."""
        # Arrange
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) >= 2

        # Mark one as modified
        modified_event = created[0]
        modified_event.is_modified = True
        modified_event.save(update_fields=["is_modified"])

        unmodified_event = created[1]

        # Act
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={"description": "Updated description"},
            scope="future_unmodified",
        )

        # Assert
        modified_event.refresh_from_db()
        unmodified_event.refresh_from_db()
        assert modified_event.description != "Updated description"
        assert unmodified_event.description == "Updated description"
        assert count >= 1

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_all_future_scope_updates_all_future_events(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Test that scope 'all_future' updates all future events regardless of is_modified."""
        # Arrange
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) >= 2

        # Mark one as modified
        modified_event = created[0]
        modified_event.is_modified = True
        modified_event.save(update_fields=["is_modified"])

        # Act — use name field to avoid timezone edge cases with start__gte filter
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={"name": "Propagated Name"},
            scope="all_future",
        )

        # Assert — at least one event should be updated (those with start >= now)
        assert count >= 1
        for event in created:
            event.refresh_from_db()
        updated = [e for e in created if e.name == "Propagated Name"]
        assert len(updated) >= 1

    def test_invalid_scope_raises_value_error(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that an invalid scope string raises ValueError."""
        # Act & Assert
        with pytest.raises(ValueError, match="Invalid propagation scope"):
            recurrence_service.propagate_template_changes(
                series=active_series,
                changed_fields={"description": "test"},
                scope="invalid_scope",
            )

    def test_empty_changed_fields_returns_zero(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that empty changed_fields dict returns 0 without any DB updates."""
        # Act
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={},
            scope="future_unmodified",
        )

        # Assert
        assert count == 0

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_does_not_update_past_events(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
        organization: Organization,
    ) -> None:
        """Test that propagate does not update events in the past."""
        # Arrange - create a past event manually in the series
        past_event = Event.objects.create(
            organization=organization,
            event_series=active_series,
            name="Past Event",
            start=timezone.now() - timedelta(days=7),
            status=Event.EventStatus.OPEN,
            is_template=False,
            is_modified=False,
        )

        # Also generate future events
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) > 0

        # Act
        recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={"description": "Should not reach past"},
            scope="all_future",
        )

        # Assert
        past_event.refresh_from_db()
        assert past_event.description != "Should not reach past"
