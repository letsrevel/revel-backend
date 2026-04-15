"""Tests for the recurrence service: template propagation.

Split from the original ``test_recurrence_service.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover materialization
and lifecycle operations. Shared fixtures live in ``conftest.py``.
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.utils import timezone
from freezegun import freeze_time

from events.models import Event, EventSeries, Organization
from events.service import recurrence_service
from events.service.recurrence_service import PropagateScope

pytestmark = pytest.mark.django_db


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
            scope=PropagateScope.FUTURE_UNMODIFIED,
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
            scope=PropagateScope.ALL_FUTURE,
        )

        # Assert — at least one event should be updated (those with start >= now)
        assert count >= 1
        for event in created:
            event.refresh_from_db()
        updated = [e for e in created if e.name == "Propagated Name"]
        assert len(updated) >= 1

    def test_none_scope_is_a_noop(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that scope=NONE is a no-op and returns 0 without touching events."""
        # Act
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={"description": "test"},
            scope=PropagateScope.NONE,
        )

        # Assert
        assert count == 0

    def test_empty_changed_fields_returns_zero(
        self,
        active_series: EventSeries,
    ) -> None:
        """Test that empty changed_fields dict returns 0 without any DB updates."""
        # Act
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={},
            scope=PropagateScope.FUTURE_UNMODIFIED,
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
            scope=PropagateScope.ALL_FUTURE,
        )

        # Assert
        past_event.refresh_from_db()
        assert past_event.description != "Should not reach past"


class TestPropagateAtomicity:
    """``propagate_template_changes`` must be all-or-nothing under failure."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_failure_mid_loop_rolls_back_prior_updates(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """If propagation raises mid-loop, all in-batch updates must roll back.

        Without ``@transaction.atomic`` on ``propagate_template_changes``, a
        save() failure on the third occurrence would leave the first two with
        the new value while the rest stay on the old value. The atomic wrapper
        guarantees the entire batch reverts so the propagation contract is
        all-or-nothing regardless of which caller invokes it.
        """
        # Arrange — generate a handful of future occurrences and snapshot
        # their original description.
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) >= 3
        original_descriptions = {e.id: e.description for e in created}

        # Patch Event.save so the third call raises while the first two
        # succeed. The atomic block must roll those two back.
        original_save = Event.save
        call_count = {"n": 0}

        def flaky_save(self: Event, *args: t.Any, **kwargs: t.Any) -> None:
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise ValidationError("simulated mid-loop failure")
            original_save(self, *args, **kwargs)

        # Act + Assert — call must raise.
        with patch.object(Event, "save", flaky_save):
            with pytest.raises(ValidationError):
                recurrence_service.propagate_template_changes(
                    series=active_series,
                    changed_fields={"description": "should be rolled back"},
                    scope=PropagateScope.ALL_FUTURE,
                )

        # Assert — no occurrence retains the new description.
        for event in created:
            event.refresh_from_db()
            assert event.description == original_descriptions[event.id], (
                f"Event {event.id} kept the in-batch change after rollback"
            )


class TestPropagateCoupledFields:
    """``propagate_template_changes`` must keep ``address`` + ``location`` consistent."""

    @freeze_time("2026-04-06 10:00:00")
    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_address_only_pulls_location_from_template(
        self,
        mock_notify: t.Any,
        active_series: EventSeries,
    ) -> None:
        """Propagating ``address`` alone must also copy ``location`` from the template."""
        # Arrange — set a location on the template, generate events, then change address.
        template = active_series.template_event
        assert template is not None
        template.location = Point(9.19, 45.4642)  # Milan
        template.address = "Piazza del Duomo 1, Milan"
        template.save(update_fields=["location", "address"])
        created = recurrence_service.generate_series_events(active_series)
        assert len(created) > 0

        # Simulate a later template edit that only changes address.
        template.address = "Piazza Navona, Rome"
        template.location = Point(12.4733, 41.8992)  # Rome
        template.save(update_fields=["location", "address"])

        # Act — caller only sent "address" in the payload.
        count = recurrence_service.propagate_template_changes(
            series=active_series,
            changed_fields={"address": "Piazza Navona, Rome"},
            scope=PropagateScope.ALL_FUTURE,
        )

        # Assert — occurrences get both address AND location from the template, not just address.
        assert count >= 1
        for event in created:
            event.refresh_from_db()
            if event.start > timezone.now():
                assert event.address == "Piazza Navona, Rome"
                assert event.location is not None
                assert abs(event.location.x - 12.4733) < 0.001
                assert abs(event.location.y - 41.8992) < 0.001
