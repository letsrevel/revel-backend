"""Tests for follower notification visibility filtering.

Verifies that non-public events (private, members-only, staff-only) do NOT
trigger follower notifications, since followers lack access to those events.
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

FOLLOWER_NOTIFICATION_TYPES = [
    NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
    NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
]


class TestFollowerNotificationVisibility:
    """Tests that follower notifications respect event visibility."""

    @pytest.mark.parametrize(
        "visibility",
        [
            Event.Visibility.UNLISTED,
            Event.Visibility.PRIVATE,
            Event.Visibility.MEMBERS_ONLY,
            Event.Visibility.STAFF_ONLY,
        ],
    )
    def test_no_follower_notification_for_non_public_event(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
        visibility: str,
    ) -> None:
        """Test that followers are NOT notified when a non-public event opens.

        Non-public events (private, members-only, staff-only) are only visible to
        specific users. Followers without explicit access should not receive
        notifications they cannot act on.
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

        event = Event.objects.create(
            organization=organization,
            name="Non-Public Event",
            slug=f"non-public-event-{visibility}",
            event_type=Event.EventType.PRIVATE,
            visibility=visibility,
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
        follower_calls = [
            c for c in mock_send.call_args_list if c.kwargs.get("notification_type") in FOLLOWER_NOTIFICATION_TYPES
        ]
        assert len(follower_calls) == 0

    def test_no_follower_notification_for_private_event_created_as_open(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that creating a private event directly as OPEN doesn't notify followers."""
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
                    name="Private Open Event",
                    slug="private-open-event",
                    event_type=Event.EventType.PRIVATE,
                    visibility=Event.Visibility.PRIVATE,
                    max_attendees=100,
                    start=timezone.now(),
                    status=Event.EventStatus.OPEN,
                )

        # Assert
        follower_calls = [
            c for c in mock_send.call_args_list if c.kwargs.get("notification_type") in FOLLOWER_NOTIFICATION_TYPES
        ]
        assert len(follower_calls) == 0

    def test_no_series_follower_notification_for_private_event(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that series followers are also excluded for non-public events."""
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            event_series=event_series,
            name="Private Series Event",
            slug="private-series-event",
            event_type=Event.EventType.PRIVATE,
            visibility=Event.Visibility.PRIVATE,
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
        follower_calls = [
            c for c in mock_send.call_args_list if c.kwargs.get("notification_type") in FOLLOWER_NOTIFICATION_TYPES
        ]
        assert len(follower_calls) == 0

    def test_public_event_still_notifies_followers(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Regression guard: public events must still notify followers."""
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            name="Public Event",
            slug="public-event-visibility-test",
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
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user
