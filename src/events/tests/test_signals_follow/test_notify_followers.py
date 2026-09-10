"""Tests for who receives follower notifications when an event opens.

Covers the recipient-selection half of handle_event_opened_notify_followers:
org vs series followers, the member/staff/owner exclusions, and the
cancelled/banned member states.

The trigger and payload rules live in test_notify_rules.py; the pre_save
status capture in test_status_capture.py.
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
