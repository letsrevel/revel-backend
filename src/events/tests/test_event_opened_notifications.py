"""Tests for EVENT_OPEN notification triggered by event status changes."""

import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, OrganizationMember
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestEventOpenedNotification:
    """Test that EVENT_OPEN notification is sent when event status changes to OPEN."""

    def test_event_opened_notification_sends_on_status_update(
        self, organization: t.Any, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that updating event status to OPEN sends notification with all required context."""
        from unittest.mock import patch

        # Setup: Make user a member so they get EVENT_OPEN notifications
        OrganizationMember.objects.create(user=nonmember_user, organization=organization)

        # Create event as DRAFT with PUBLIC visibility so members get notified
        event = Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.DRAFT,
        )

        # Clear any notifications created during setup
        Notification.objects.all().delete()

        with patch("notifications.tasks.dispatch_notifications_batch.delay") as mock_dispatch:
            # Update to OPEN and capture on_commit callbacks
            with django_capture_on_commit_callbacks(execute=True):
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

            # Verify notifications were created (one for owner, one for member)
            notifications = Notification.objects.filter(notification_type=NotificationType.EVENT_OPEN)
            assert notifications.count() == 2

            # Verify dispatch was called with notification IDs
            assert mock_dispatch.called
            notification_ids = mock_dispatch.call_args[0][0]
            assert len(notification_ids) == 2

            # Check all notifications have correct type and context
            for notification in notifications:
                assert notification.notification_type == NotificationType.EVENT_OPEN

                # Check context has all required fields
                context = notification.context
                assert "event_id" in context
                assert "event_name" in context
                assert "event_description" in context
                assert "event_start" in context
                assert "event_end" in context
                assert "event_location" in context
                assert "organization_id" in context
                assert "organization_name" in context
                assert "rsvp_required" in context
                assert "tickets_available" in context
                assert "questionnaire_required" in context

                # Verify values are correct
                assert context["event_id"] == str(event.id)
                assert context["event_name"] == event.name
                assert context["organization_id"] == str(event.organization.id)
                assert context["organization_name"] == event.organization.name

            # Verify both users received notifications
            notified_users = {n.user for n in notifications}
            assert nonmember_user in notified_users
            assert organization.owner in notified_users

    def test_event_opened_notification_not_sent_when_status_unchanged(
        self, organization: t.Any, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that notification is not sent when status is not explicitly updated."""
        from unittest.mock import patch

        # Create event as OPEN initially
        event = Event.objects.create(
            organization=organization,
            name="Already Open Event",
            slug="already-open-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.OPEN,
        )

        with patch("notifications.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                # Update a different field (not status and not a watched field)
                event.max_attendees = 200
                event.save(update_fields=["max_attendees"])

            # Verify notification was NOT sent
            assert not mock_send.called

    def test_event_opened_notification_not_sent_when_created_as_draft(
        self, organization: t.Any, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that notification is not sent when event is created as DRAFT."""
        from unittest.mock import patch

        with patch("notifications.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                # Create event as DRAFT
                Event.objects.create(
                    organization=organization,
                    name="Draft Event",
                    slug="draft-event",
                    event_type=Event.EventType.PUBLIC,
                    max_attendees=100,
                    start=timezone.now(),
                    status=Event.EventStatus.DRAFT,
                )

            # Verify notification was NOT sent
            assert not mock_send.called

    def test_event_opened_notification_sent_when_created_as_open(
        self, organization: t.Any, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that notification is sent when event is created directly as OPEN."""
        from unittest.mock import patch

        # Setup: Make user a member
        OrganizationMember.objects.create(user=nonmember_user, organization=organization)

        # Clear any notifications created during setup
        Notification.objects.all().delete()

        with patch("notifications.tasks.dispatch_notifications_batch.delay") as mock_dispatch:
            with django_capture_on_commit_callbacks(execute=True):
                # Create event as OPEN with PUBLIC visibility so members get notified
                Event.objects.create(
                    organization=organization,
                    name="Opened Event",
                    slug="opened-event",
                    event_type=Event.EventType.PUBLIC,
                    visibility=Event.Visibility.PUBLIC,
                    max_attendees=100,
                    start=timezone.now(),
                    status=Event.EventStatus.OPEN,
                )

            # Verify notifications were created (one for owner, one for member)
            notifications = Notification.objects.filter(notification_type=NotificationType.EVENT_OPEN)
            assert notifications.count() == 2

            # Verify dispatch was called
            assert mock_dispatch.called

            # Check all notifications have correct type
            for notification in notifications:
                assert notification.notification_type == NotificationType.EVENT_OPEN

            # Verify both users received notifications
            notified_users = {n.user for n in notifications}
            assert nonmember_user in notified_users
            assert organization.owner in notified_users

    def test_event_opened_context_includes_location_from_address(
        self, organization: t.Any, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that event location comes from address field when available."""
        from unittest.mock import patch

        # Setup: Make user a member
        OrganizationMember.objects.create(user=nonmember_user, organization=organization)

        # Create event as DRAFT with address
        event = Event.objects.create(
            organization=organization,
            name="Event with Address",
            slug="event-with-address",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            address="123 Main St, City, State 12345",
            status=Event.EventStatus.DRAFT,
        )

        # Clear any notifications created during setup
        Notification.objects.all().delete()

        with patch("notifications.tasks.dispatch_notifications_batch.delay") as mock_dispatch:
            with django_capture_on_commit_callbacks(execute=True):
                # Update to OPEN
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

            # Verify notifications were created
            notifications = Notification.objects.filter(notification_type=NotificationType.EVENT_OPEN)
            assert notifications.exists()
            assert mock_dispatch.called

            # Check location in context (check any notification)
            notification = notifications.first()
            assert notification is not None
            assert notification.context["event_location"] == "123 Main St, City, State 12345"

    def test_event_opened_context_boolean_flags(
        self, organization: t.Any, nonmember_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Test that boolean flags are correctly set in context."""
        from unittest.mock import patch

        # Setup: Make user a member
        OrganizationMember.objects.create(user=nonmember_user, organization=organization)

        # Create event with RSVP mode (no tickets required)
        event = Event.objects.create(
            organization=organization,
            name="RSVP Event",
            slug="rsvp-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            requires_ticket=False,
            status=Event.EventStatus.DRAFT,
        )

        # Clear any notifications created during setup
        Notification.objects.all().delete()

        with patch("notifications.tasks.dispatch_notifications_batch.delay") as mock_dispatch:
            with django_capture_on_commit_callbacks(execute=True):
                # Update to OPEN
                event.status = Event.EventStatus.OPEN
                event.save(update_fields=["status"])

            # Verify notifications were created
            notifications = Notification.objects.filter(notification_type=NotificationType.EVENT_OPEN)
            assert notifications.exists()
            assert mock_dispatch.called

            # Verify boolean flags (check any notification)
            notification = notifications.first()
            assert notification is not None
            context = notification.context
            assert context["rsvp_required"] is True
            assert context["tickets_available"] is False
            assert context["questionnaire_required"] is False
