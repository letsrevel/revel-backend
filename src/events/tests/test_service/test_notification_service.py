"""Tests for notification service functions."""

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    UserEventPreferences,
    UserEventSeriesPreferences,
    UserOrganizationPreferences,
)
from events.service.notification_service import NotificationType, get_eligible_users_for_event_notification

pytestmark = pytest.mark.django_db


class TestGetEligibleUsersForEventNotification:
    """Test the get_eligible_users_for_event_notification function."""

    def test_includes_users_with_event_specific_preferences(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with event-specific preferences are included."""
        # Create event-specific preference with subscription and notification enabled
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        assert nonmember_user in eligible_users

    def test_includes_users_with_event_series_preferences(
        self, organization: Organization, event_series: EventSeries, nonmember_user: RevelUser
    ) -> None:
        """Test that users with event series preferences are included when event belongs to series."""
        from django.utils import timezone

        # Create event that belongs to the event series
        event = Event.objects.create(
            organization=organization,
            name="Series Event",
            slug="series-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now(),
            status="open",
            event_series=event_series,
        )

        # Create event series preference with subscription and notification enabled
        UserEventSeriesPreferences.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_subscribed=True,
            notify_on_new_events=True,
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

        assert nonmember_user in eligible_users

    def test_includes_users_with_organization_preferences(
        self, event: Event, nonmember_user: RevelUser, organization: Organization
    ) -> None:
        """Test that users with organization preferences are included."""
        # Create organization preference with subscription and notification enabled
        UserOrganizationPreferences.objects.create(
            user=nonmember_user,
            organization=organization,
            is_subscribed=True,
            notify_on_new_events=True,
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

        assert nonmember_user in eligible_users

    def test_excludes_users_who_silenced_all_notifications(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users who silenced all notifications are excluded."""
        # Create preference with notifications silenced
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
            silence_all_notifications=True,  # User silenced everything
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        assert nonmember_user not in eligible_users

    def test_excludes_users_who_are_not_subscribed(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that unsubscribed users are excluded."""
        # Create preference but user is not subscribed
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=False,  # Not subscribed
            notify_on_potluck_updates=True,
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        assert nonmember_user not in eligible_users

    def test_excludes_users_with_specific_notification_disabled(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users who disabled specific notification type are excluded."""
        # Create preference with potluck notifications disabled
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=False,  # Disabled for potluck
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        assert nonmember_user not in eligible_users

    def test_respects_notification_type_filter(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that only users who enabled the specific notification type are included."""
        # User has potluck notifications enabled but not new event notifications
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
        )

        # Should be included for POTLUCK_UPDATE
        eligible_for_potluck = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)
        assert nonmember_user in eligible_for_potluck

        # Should NOT be included for EVENT_OPEN (no notify_on_new_events in event preferences)
        eligible_for_event_open = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)
        assert nonmember_user not in eligible_for_event_open

    def test_handles_event_without_series(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that function handles events without an event series gracefully."""
        # Ensure event has no series
        event.event_series = None
        event.save()

        # Create event preference
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
        )

        # Should work without errors
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        assert nonmember_user in eligible_users

    def test_returns_unique_users(self, event: Event, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test that users are not duplicated if they match multiple criteria."""
        # Create both event and organization preferences for the same user
        UserEventPreferences.objects.create(
            user=nonmember_user,
            event=event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
        )
        UserOrganizationPreferences.objects.create(
            user=nonmember_user,
            organization=organization,
            is_subscribed=True,
            notify_on_new_events=True,
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)

        # User should appear only once
        user_ids = list(eligible_users.values_list("id", flat=True))
        assert user_ids.count(nonmember_user.id) == 1

    def test_multiple_users_with_different_preferences(
        self, event: Event, organization: Organization, event_series: EventSeries
    ) -> None:
        """Test filtering multiple users with different preference levels."""
        from django.utils import timezone

        # Create event with series
        series_event = Event.objects.create(
            organization=organization,
            name="Series Event",
            slug="series-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now(),
            status="open",
            event_series=event_series,
        )

        # User 1: event-specific preference
        user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
        UserEventPreferences.objects.create(
            user=user1,
            event=series_event,
            is_subscribed=True,
            notify_on_potluck_updates=True,
        )

        # User 2: series-specific preference
        user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
        UserEventSeriesPreferences.objects.create(
            user=user2,
            event_series=event_series,
            is_subscribed=True,
            notify_on_new_events=False,  # Disabled
        )

        # User 3: organization-specific preference
        user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")
        UserOrganizationPreferences.objects.create(
            user=user3,
            organization=organization,
            is_subscribed=True,
            notify_on_new_events=True,
        )

        eligible_users = get_eligible_users_for_event_notification(series_event, NotificationType.POTLUCK_UPDATE)

        # Only user1 should be included (has potluck notifications enabled)
        assert user1 in eligible_users
        assert user2 not in eligible_users
        assert user3 not in eligible_users

    def test_event_series_preference_with_correct_related_name(
        self, organization: Organization, event_series: EventSeries, nonmember_user: RevelUser
    ) -> None:
        """Test that event series preferences work with the correct related name.

        This specifically tests the bug fix where the related name should be
        'usereventseriespreferences_preferences' not 'usereventseries_preferences'.
        """
        from django.utils import timezone

        # Create event in the series
        event = Event.objects.create(
            organization=organization,
            name="Series Event",
            slug="series-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now(),
            status="open",
            event_series=event_series,
        )

        # Create series preference
        UserEventSeriesPreferences.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_subscribed=True,
            notify_on_new_events=True,
        )

        # This should not raise a FieldError
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

        # User should be included
        assert nonmember_user in eligible_users
