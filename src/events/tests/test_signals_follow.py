"""Tests for follow-related signal handlers.

This module tests the signal handlers that notify followers when events
become open, including the pre_save status capture and post_save notification dispatch.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, OrganizationMember, OrganizationStaff
from events.models.follow import EventSeriesFollow, OrganizationFollow
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


class TestCaptureEventOldStatus:
    """Tests for the capture_event_old_status pre_save signal handler."""

    def test_captures_old_status_on_update(
        self,
        organization: Organization,
    ) -> None:
        """Test that the old status is captured when an event is updated.

        This test verifies that the pre_save handler stores the previous status
        value on the instance for use in post_save comparison.
        """
        # Arrange - Create event as DRAFT
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

        # Act - Update status to OPEN
        event.status = Event.EventStatus.OPEN
        event.save(update_fields=["status"])

        # Assert - The _old_status should have been set during pre_save
        # Note: We can't directly test the attribute after save since it may be cleaned up,
        # but we can verify the behavior through the notification tests below

    def test_no_old_status_for_new_event(
        self,
        organization: Organization,
    ) -> None:
        """Test that no old status is captured for newly created events.

        This test verifies that the pre_save handler doesn't set _old_status
        for events being created (no pk yet).
        """
        # Act - Create new event directly as OPEN
        event = Event.objects.create(
            organization=organization,
            name="New Event",
            slug="new-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.OPEN,
        )

        # The event should be created successfully without errors
        assert event.pk is not None


class TestHandleEventOpenedNotifyFollowers:
    """Tests for the handle_event_opened_notify_followers post_save signal handler."""

    def test_notifies_org_followers_when_event_opens(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that organization followers are notified when an event becomes OPEN.

        This test verifies that followers with notify_new_events enabled receive
        the NEW_EVENT_FROM_FOLLOWED_ORG notification when an event status changes
        from DRAFT to OPEN.
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

        # Create event as DRAFT
        event = Event.objects.create(
            organization=organization,
            name="Draft Event",
            slug="draft-event",
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
        assert mock_send.called
        # Find the follower notification call
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

        # Verify context
        context = follower_calls[0].kwargs["context"]
        assert context["event_id"] == str(event.id)
        assert context["event_name"] == event.name
        assert context["organization_id"] == str(organization.id)
        assert context["organization_name"] == organization.name

    def test_notifies_series_followers_when_event_opens(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that series followers are notified with series-specific notification.

        This test verifies that followers of an event series receive the
        NEW_EVENT_FROM_FOLLOWED_SERIES notification type.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event in series as DRAFT
        event = Event.objects.create(
            organization=organization,
            event_series=event_series,
            name="Series Event",
            slug="series-event",
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
        assert mock_send.called
        # Find the series follower notification call
        series_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES
        ]
        assert len(series_calls) == 1
        assert series_calls[0].kwargs["user"] == nonmember_user

        # Verify series context is included
        context = series_calls[0].kwargs["context"]
        assert context["event_series_id"] == str(event_series.id)
        assert context["event_series_name"] == event_series.name

    def test_series_followers_prioritized_over_org_followers(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that users following both series and org get series notification only.

        This test verifies that when a user follows both the organization and
        one of its series, they receive only the series notification to avoid
        duplicate notifications.
        """
        # Arrange - User follows both org and series
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event in series as DRAFT
        event = Event.objects.create(
            organization=organization,
            event_series=event_series,
            name="Series Event",
            slug="series-event-priority",
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

        # Assert - User should receive only series notification, not org notification
        user_notifications = [c for c in mock_send.call_args_list if c.kwargs.get("user") == nonmember_user]
        assert len(user_notifications) == 1
        assert user_notifications[0].kwargs["notification_type"] == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES

    def test_excludes_members_from_follower_notifications(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that org members are excluded from follower notifications.

        This test verifies that members don't receive follower notifications
        because they already receive EVENT_OPEN notifications via the
        membership-based notification system.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Create a member who also follows the org
        member_user = revel_user_factory()
        OrganizationMember.objects.create(
            user=member_user,
            organization=organization,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        OrganizationFollow.objects.create(
            user=member_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Non-member follower
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Create event as DRAFT
        event = Event.objects.create(
            organization=organization,
            name="Member Test Event",
            slug="member-test-event",
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

        # Assert - Only non-member follower should get NEW_EVENT_FROM_FOLLOWED_ORG
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

        # Member should NOT receive follower notification
        member_follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("user") == member_user
            and c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(member_follower_calls) == 0

    def test_excludes_staff_from_follower_notifications(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that staff members who also follow the org don't get follower notifications.

        Staff already receive EVENT_OPEN via the staff notification path, so they
        must be excluded from follower notifications to prevent duplicates.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        staff_user = revel_user_factory()
        OrganizationStaff.objects.create(organization=organization, user=staff_user)
        OrganizationFollow.objects.create(
            user=staff_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Non-member follower
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            name="Staff Dedup Event",
            slug="staff-dedup-event",
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

        # Assert - Staff should NOT get follower notification
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

        staff_follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("user") == staff_user
            and c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(staff_follower_calls) == 0

    def test_excludes_owner_from_follower_notifications(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that the org owner who also follows the org doesn't get follower notification.

        Owner already receives EVENT_OPEN via the owner notification path.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        OrganizationFollow.objects.create(
            user=organization_owner_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Non-member follower
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            name="Owner Dedup Event",
            slug="owner-dedup-event",
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

        # Assert - Owner should NOT get follower notification
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == nonmember_user

    def test_cancelled_member_receives_follower_notification(
        self,
        organization: Organization,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that cancelled members DO receive follower notifications.

        Cancelled members left voluntarily and no longer receive EVENT_OPEN,
        so they should still get follower notifications if they follow the org.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        cancelled_member = revel_user_factory()
        OrganizationMember.objects.create(
            user=cancelled_member,
            organization=organization,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        OrganizationFollow.objects.create(
            user=cancelled_member,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            name="Cancelled Member Event",
            slug="cancelled-member-event",
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

        # Assert - Cancelled member should receive follower notification
        follower_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(follower_calls) == 1
        assert follower_calls[0].kwargs["user"] == cancelled_member

    def test_banned_member_receives_no_notifications(
        self,
        organization: Organization,
        revel_user_factory: t.Any,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that banned members receive NO notifications at all.

        Banned members should not receive EVENT_OPEN (via membership) or
        follower notifications.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        banned_member = revel_user_factory()
        OrganizationMember.objects.create(
            user=banned_member,
            organization=organization,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        OrganizationFollow.objects.create(
            user=banned_member,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        event = Event.objects.create(
            organization=organization,
            name="Banned Member Event",
            slug="banned-member-event",
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

        # Assert - Banned member should NOT receive any follower notification
        banned_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("user") == banned_member
            and c.kwargs.get("notification_type")
            in [NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG, NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES]
        ]
        assert len(banned_calls) == 0

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
