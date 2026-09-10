"""Tests for when a follower notification fires and what it carries.

Covers the trigger half of handle_event_opened_notify_followers: status
transitions, the notify_new_events preference, archived follows, the context
payload, and the combined org+series case.

Recipient selection lives in test_notify_followers.py.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization
from events.models.follow import EventSeriesFollow, OrganizationFollow
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


class TestEventOpenedNotificationRules:
    """Tests for the trigger, preference and context rules of the open-event signal."""

    def test_no_notification_when_status_unchanged(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that no notification is sent when status doesn't change.

        This test verifies that updating other fields on an OPEN event
        doesn't trigger follower notifications.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event as OPEN
        event = Event.objects.create(
            organization=organization,
            name="Already Open Event",
            slug="already-open",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.OPEN,
        )

        # Act - Update a different field
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.max_attendees = 200
                event.save(update_fields=["max_attendees"])

        # Assert - No follower notifications should be sent
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type")
            in [
                NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
                NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
            ]
        ]
        assert len(follower_calls) == 0

    def test_no_notification_when_created_as_draft(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that no follower notification is sent when event created as DRAFT.

        This test verifies that creating a draft event doesn't notify followers.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                Event.objects.create(
                    organization=organization,
                    name="Draft Event",
                    slug="draft-no-notify",
                    event_type=Event.EventType.PUBLIC,
                    visibility=Event.Visibility.PUBLIC,
                    max_attendees=100,
                    start=timezone.now(),
                    status=Event.EventStatus.DRAFT,
                )

        # Assert - No follower notifications
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type")
            in [
                NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
                NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
            ]
        ]
        assert len(follower_calls) == 0

    def test_notification_sent_when_created_directly_as_open(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that followers are notified when event is created directly as OPEN.

        This test verifies that creating an event directly with OPEN status
        triggers follower notifications.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                Event.objects.create(
                    organization=organization,
                    name="New Open Event",
                    slug="new-open-event",
                    event_type=Event.EventType.PUBLIC,
                    visibility=Event.Visibility.PUBLIC,
                    max_attendees=100,
                    start=timezone.now(),
                    status=Event.EventStatus.OPEN,
                )

        # Assert - Follower should be notified
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

    def test_respects_notify_new_events_preference(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that followers with notifications disabled don't receive notifications.

        This test verifies that the notify_new_events preference is respected.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        second_user = revel_user_factory()

        # First user has notifications enabled
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )
        # Second user has notifications disabled
        OrganizationFollow.objects.create(
            user=second_user,
            organization=organization,
            is_archived=False,
            notify_new_events=False,
        )

        # Create event as DRAFT
        event = Event.objects.create(
            organization=organization,
            name="Pref Test Event",
            slug="pref-test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.DRAFT,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

        # Assert - Only first user should receive notification
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

    def test_archived_follows_not_notified(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that archived (unfollowed) users don't receive notifications.

        This test verifies that users who have unfollowed are excluded from
        notifications even if they had notifications enabled before.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=True,  # Archived (unfollowed)
            notify_new_events=True,
        )

        # Create event as DRAFT
        event = Event.objects.create(
            organization=organization,
            name="Archived Test Event",
            slug="archived-test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.DRAFT,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

        # Assert - No follower notifications (archived user excluded)
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 0

    def test_context_includes_event_location_from_address(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that event location is included in notification context.

        This test verifies that the address field is properly passed in the
        notification context.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event with address
        event = Event.objects.create(
            organization=organization,
            name="Location Test Event",
            slug="location-test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            address="123 Main St, City, State 12345",
            status=Event.EventStatus.DRAFT,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

        # Assert
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        context = follower_calls[0].kwargs["context"]
        assert context["event_location"] == "123 Main St, City, State 12345"

    def test_no_notification_when_status_changes_to_non_open(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that changing status to non-OPEN doesn't notify followers.

        This test verifies that only OPEN status triggers follower notifications,
        not other status changes like CLOSED or CANCELLED.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event as OPEN
        event = Event.objects.create(
            organization=organization,
            name="Close Test Event",
            slug="close-test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.OPEN,
        )

        # Act - Change to CLOSED
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.CLOSED
                event.save(update_fields=["status"])

        # Assert - No follower notifications
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type")
            in [
                NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
                NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
            ]
        ]
        assert len(follower_calls) == 0

    def test_both_org_and_series_followers_notified(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that both org and series followers are notified appropriately.

        This test verifies that for an event in a series:
        - Series followers get NEW_EVENT_FROM_FOLLOWED_SERIES
        - Org-only followers get NEW_EVENT_FROM_FOLLOWED_ORG
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        series_follower = revel_user_factory()
        org_only_follower = revel_user_factory()

        # Series follower
        EventSeriesFollow.objects.create(
            user=series_follower,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        # Org-only follower (does not follow the series)
        OrganizationFollow.objects.create(
            user=org_only_follower,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event in series
        event = Event.objects.create(
            organization=organization,
            event_series=event_series,
            name="Multi Follower Event",
            slug="multi-follower-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.DRAFT,
        )

        # Act
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

        # Assert
        series_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES
        ]
        org_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]

        # Series follower gets series notification
        assert len(series_calls) == 1
        assert series_calls[0].kwargs["user"] == series_follower

        # Org-only follower gets org notification
        assert len(org_calls) == 1
        assert org_calls[0].kwargs["user"] == org_only_follower
