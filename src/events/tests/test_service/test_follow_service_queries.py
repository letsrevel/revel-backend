"""Tests for follow service query functions.

This module tests the query and listing functions for follows, including
is_following checks, get_followers queries, and notification recipient selection.
"""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import EventSeries, Organization, OrganizationMember
from events.models.follow import EventSeriesFollow, OrganizationFollow
from events.service import follow_service
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


class TestIsFollowing:
    """Tests for is_following_organization and is_following_event_series functions."""

    def test_is_following_organization_true(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that is_following_organization returns True for active follow.

        This test verifies the helper correctly detects active follow relationships.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
        )

        # Act & Assert
        assert follow_service.is_following_organization(nonmember_user, organization) is True

    def test_is_following_organization_false_not_following(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that is_following_organization returns False when not following.

        This test verifies the helper correctly returns False when no follow exists.
        """
        # Act & Assert
        assert follow_service.is_following_organization(nonmember_user, organization) is False

    def test_is_following_organization_false_when_archived(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that is_following_organization returns False for archived follow.

        This test verifies that archived (unfollowed) relationships are not
        considered active follows.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=True,
        )

        # Act & Assert
        assert follow_service.is_following_organization(nonmember_user, organization) is False

    def test_is_following_event_series_true(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that is_following_event_series returns True for active follow.

        This test verifies the helper correctly detects active follow relationships.
        """
        # Arrange
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
        )

        # Act & Assert
        assert follow_service.is_following_event_series(nonmember_user, event_series) is True

    def test_is_following_event_series_false_when_archived(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that is_following_event_series returns False for archived follow.

        This test verifies that archived follows are not considered active.
        """
        # Arrange
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=True,
        )

        # Act & Assert
        assert follow_service.is_following_event_series(nonmember_user, event_series) is False


class TestGetFollowersForNewEventNotification:
    """Tests for get_followers_for_new_event_notification function."""

    def test_returns_org_followers_for_org_only_event(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that org followers are notified for events without a series.

        This test verifies that when an event is created without belonging to
        a series, organization followers receive the correct notification type.
        """
        # Arrange
        second_user = revel_user_factory()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )
        OrganizationFollow.objects.create(
            user=second_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series=None))

        # Assert
        assert len(results) == 2
        for user, notification_type in results:
            assert notification_type == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        notified_users = {user for user, _ in results}
        assert nonmember_user in notified_users
        assert second_user in notified_users

    def test_returns_series_followers_for_series_event(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that series followers get series notification type.

        This test verifies that when an event belongs to a series, followers
        of that series receive the series-specific notification type.
        """
        # Arrange
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series))

        # Assert
        assert len(results) == 1
        user, notification_type = results[0]
        assert user == nonmember_user
        assert notification_type == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES

    def test_series_followers_prioritized_over_org_followers(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that users following both series and org get series notification only.

        This test verifies that when a user follows both an organization and one
        of its series, they receive only the series notification to avoid duplicates.
        """
        # Arrange - User follows both org and series
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

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series))

        # Assert - User receives only series notification
        assert len(results) == 1
        user, notification_type = results[0]
        assert user == nonmember_user
        assert notification_type == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES

    def test_excludes_members_from_follower_notifications(
        self,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that org members are excluded from follower notifications.

        This test verifies that members don't receive follower notifications
        because they already get EVENT_OPEN notifications via the membership system.
        This prevents duplicate notifications.
        """
        # Arrange - nonmember_user is a member and org follower
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

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series=None))

        # Assert - Only non-member follower notified
        assert len(results) == 1
        user, _ = results[0]
        assert user == nonmember_user

    def test_excludes_paused_members_from_follower_notifications(
        self,
        organization: Organization,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that paused members are also excluded from follower notifications.

        This test verifies that paused members (who still get EVENT_OPEN) are
        excluded from follower notifications.
        """
        # Arrange
        paused_member = revel_user_factory()
        OrganizationMember.objects.create(
            user=paused_member,
            organization=organization,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )
        OrganizationFollow.objects.create(
            user=paused_member,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series=None))

        # Assert
        assert len(results) == 0

    def test_respects_notify_new_events_preference(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that followers with notifications disabled are excluded.

        This test verifies that the notify_new_events preference is respected
        when determining which followers to notify.
        """
        # Arrange
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

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series=None))

        # Assert
        assert len(results) == 1
        user, _ = results[0]
        assert user == nonmember_user

    def test_excludes_archived_follows(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that archived (unfollowed) relationships are excluded.

        This test verifies that users who have unfollowed don't receive
        notifications even if they had notifications enabled before unfollowing.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=True,  # Archived
            notify_new_events=True,
        )

        # Act
        results = list(follow_service.get_followers_for_new_event_notification(organization, event_series=None))

        # Assert
        assert len(results) == 0


class TestGetUserFollows:
    """Tests for get_user_followed_organizations and get_user_followed_event_series."""

    def test_get_user_followed_organizations_returns_active_only(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that only active organization follows are returned.

        This test verifies that archived follows are not included in the results.
        """
        # Arrange - Create second org to have both active and archived follows
        second_org = Organization.objects.create(
            name="Second Org",
            slug="second-org",
            owner=revel_user_factory(),
            visibility=Organization.Visibility.PUBLIC,
        )
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
        )
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=second_org,
            is_archived=True,  # Archived
        )

        # Act
        results = follow_service.get_user_followed_organizations(nonmember_user)

        # Assert
        assert results.count() == 1
        result = results.first()
        assert result is not None
        assert result.organization == organization

    def test_get_user_followed_event_series_returns_active_only(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that only active event series follows are returned.

        This test verifies that archived follows are not included.
        """
        # Arrange
        second_series = EventSeries.objects.create(
            organization=organization,
            name="Second Series",
            slug="second-series",
        )
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
        )
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=second_series,
            is_archived=True,  # Archived
        )

        # Act
        results = follow_service.get_user_followed_event_series(nonmember_user)

        # Assert
        assert results.count() == 1
        result = results.first()
        assert result is not None
        assert result.event_series == event_series


class TestGetFollowers:
    """Tests for get_organization_followers and get_event_series_followers."""

    def test_get_organization_followers_returns_active_only(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that only active organization followers are returned.

        This test verifies that archived follows are excluded when listing followers.
        """
        # Arrange
        second_user = revel_user_factory()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
        )
        OrganizationFollow.objects.create(
            user=second_user,
            organization=organization,
            is_archived=True,  # Archived
        )

        # Act
        results = follow_service.get_organization_followers(organization)

        # Assert
        assert results.count() == 1
        result = results.first()
        assert result is not None
        assert result.user == nonmember_user

    def test_get_event_series_followers_returns_active_only(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        revel_user_factory: t.Any,
    ) -> None:
        """Test that only active event series followers are returned.

        This test verifies that archived follows are excluded.
        """
        # Arrange
        second_user = revel_user_factory()
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
        )
        EventSeriesFollow.objects.create(
            user=second_user,
            event_series=event_series,
            is_archived=True,  # Archived
        )

        # Act
        results = follow_service.get_event_series_followers(event_series)

        # Assert
        assert results.count() == 1
        result = results.first()
        assert result is not None
        assert result.user == nonmember_user
