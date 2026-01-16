"""Tests for the follow service.

This module tests the business logic for following organizations and event series,
including notification dispatch, reactivation of archived follows, and preference updates.
"""

import typing as t
from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import EventSeries, Organization
from events.models.follow import EventSeriesFollow, OrganizationFollow
from events.service import follow_service
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


class TestFollowOrganization:
    """Tests for follow_organization function."""

    def test_follow_organization_success(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that a user can successfully follow a visible organization.

        This test verifies that the follow record is created with correct default
        notification preferences and that the organization is attached for serialization.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_organization(nonmember_user, organization)

        # Assert
        assert follow.user == nonmember_user
        assert follow.organization == organization
        assert follow.notify_new_events is True
        assert follow.notify_announcements is True
        assert follow.is_archived is False

    def test_follow_organization_with_custom_preferences(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that custom notification preferences are respected when following.

        This test verifies that users can opt out of specific notification types
        when following an organization.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_organization(
                    nonmember_user,
                    organization,
                    notify_new_events=False,
                    notify_announcements=True,
                )

        # Assert
        assert follow.notify_new_events is False
        assert follow.notify_announcements is True

    def test_follow_organization_sends_notification_to_staff(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that following an organization sends notification to org staff.

        This test verifies that when a user follows an organization, the appropriate
        ORGANIZATION_FOLLOWED notification is dispatched to eligible staff members.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                follow_service.follow_organization(nonmember_user, organization)

        # Assert
        assert mock_send.called
        # Find the call with ORGANIZATION_FOLLOWED notification type
        org_follow_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.ORGANIZATION_FOLLOWED
        ]
        assert len(org_follow_calls) >= 1

        # Verify context has required fields
        context = org_follow_calls[0].kwargs["context"]
        assert context["organization_id"] == str(organization.id)
        assert context["organization_name"] == organization.name
        assert context["follower_id"] == str(nonmember_user.id)
        assert context["follower_name"] == nonmember_user.display_name
        assert context["follower_email"] == nonmember_user.email

    def test_follow_organization_reactivates_archived_follow(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that following after unfollowing reactivates the archived record.

        This test verifies that when a user re-follows an organization they previously
        unfollowed, the existing archived follow record is reactivated rather than
        creating a new one.
        """
        # Arrange - Create an archived follow
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        existing_follow = OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=True,
            notify_new_events=False,
            notify_announcements=False,
        )
        original_id = existing_follow.id

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_organization(
                    nonmember_user,
                    organization,
                    notify_new_events=True,
                    notify_announcements=True,
                )

        # Assert - Same record reactivated with new preferences
        assert follow.id == original_id
        assert follow.is_archived is False
        assert follow.notify_new_events is True
        assert follow.notify_announcements is True
        assert OrganizationFollow.objects.filter(user=nonmember_user, organization=organization).count() == 1

    def test_follow_organization_already_following_raises_error(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that following an already-followed organization raises an error.

        This test verifies that attempting to follow an organization that the user
        is already actively following results in an appropriate HTTP error.
        """
        # Arrange - Create active follow
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            with patch("events.service.follow_service.notification_requested.send"):
                follow_service.follow_organization(nonmember_user, organization)

        assert exc_info.value.status_code == 400
        assert "Already following" in str(exc_info.value)

    def test_follow_organization_not_visible_raises_404(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that following an invisible organization raises a 404 error.

        This test verifies that users cannot follow organizations they don't have
        visibility access to (e.g., private orgs they're not members of).
        """
        # Arrange - Organization is private by default
        organization.visibility = Organization.Visibility.PRIVATE
        organization.save()

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.follow_organization(nonmember_user, organization)

        assert exc_info.value.status_code == 404
        assert "Organization not found" in str(exc_info.value)


class TestUnfollowOrganization:
    """Tests for unfollow_organization function."""

    def test_unfollow_organization_archives_follow(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that unfollowing an organization archives the follow record.

        This test verifies that the follow record is soft-deleted (archived)
        rather than permanently deleted to preserve history.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
        )

        # Act
        follow_service.unfollow_organization(nonmember_user, organization)

        # Assert
        follow = OrganizationFollow.objects.get(user=nonmember_user, organization=organization)
        assert follow.is_archived is True

    def test_unfollow_organization_not_following_raises_error(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that unfollowing a non-followed organization raises an error.

        This test verifies that attempting to unfollow an organization the user
        is not currently following results in an appropriate HTTP error.
        """
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.unfollow_organization(nonmember_user, organization)

        assert exc_info.value.status_code == 400
        assert "Not following" in str(exc_info.value)

    def test_unfollow_organization_already_archived_raises_error(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that unfollowing an already-archived follow raises an error.

        This test verifies that users cannot unfollow an organization they've
        already unfollowed (archived follow record).
        """
        # Arrange - Create archived follow
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=True,
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.unfollow_organization(nonmember_user, organization)

        assert exc_info.value.status_code == 400
        assert "Not following" in str(exc_info.value)


class TestUpdateOrganizationFollowPreferences:
    """Tests for update_organization_follow_preferences function."""

    def test_update_preferences_success(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that notification preferences can be updated successfully.

        This test verifies that users can change their notification preferences
        for an organization they're following.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
            notify_announcements=True,
        )

        # Act
        follow = follow_service.update_organization_follow_preferences(
            nonmember_user,
            organization,
            notify_new_events=False,
            notify_announcements=False,
        )

        # Assert
        assert follow.notify_new_events is False
        assert follow.notify_announcements is False

    def test_update_preferences_partial_update(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that partial updates only change specified fields.

        This test verifies that when only some preferences are provided,
        the others remain unchanged.
        """
        # Arrange
        OrganizationFollow.objects.create(
            user=nonmember_user,
            organization=organization,
            is_archived=False,
            notify_new_events=True,
            notify_announcements=True,
        )

        # Act - Only update notify_new_events
        follow = follow_service.update_organization_follow_preferences(
            nonmember_user,
            organization,
            notify_new_events=False,
        )

        # Assert
        assert follow.notify_new_events is False
        assert follow.notify_announcements is True  # Unchanged

    def test_update_preferences_not_following_raises_error(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that updating preferences for non-followed org raises error.

        This test verifies that users cannot update preferences for an
        organization they're not following.
        """
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.update_organization_follow_preferences(
                nonmember_user,
                organization,
                notify_new_events=False,
            )

        assert exc_info.value.status_code == 400
        assert "Not following" in str(exc_info.value)


class TestFollowEventSeries:
    """Tests for follow_event_series function."""

    def test_follow_event_series_success(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that a user can successfully follow a visible event series.

        This test verifies that the follow record is created with correct default
        notification preferences and that the series is attached for serialization.
        """
        # Arrange - EventSeries visibility is determined by organization visibility
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_event_series(nonmember_user, event_series)

        # Assert
        assert follow.user == nonmember_user
        assert follow.event_series == event_series
        assert follow.notify_new_events is True
        assert follow.is_archived is False

    def test_follow_event_series_with_notifications_disabled(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that following with notifications disabled is respected.

        This test verifies that users can opt out of notifications when following.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_event_series(
                    nonmember_user,
                    event_series,
                    notify_new_events=False,
                )

        # Assert
        assert follow.notify_new_events is False

    def test_follow_event_series_sends_notification(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that following a series sends notification to org staff.

        This test verifies that the EVENT_SERIES_FOLLOWED notification is
        dispatched when a user follows an event series.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Act
        with patch("events.service.follow_service.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                follow_service.follow_event_series(nonmember_user, event_series)

        # Assert
        assert mock_send.called
        series_follow_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.EVENT_SERIES_FOLLOWED
        ]
        assert len(series_follow_calls) >= 1

        context = series_follow_calls[0].kwargs["context"]
        assert context["event_series_id"] == str(event_series.id)
        assert context["event_series_name"] == event_series.name
        assert context["follower_id"] == str(nonmember_user.id)

    def test_follow_event_series_reactivates_archived(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test that re-following reactivates archived follow record.

        This test verifies that when a user re-follows a series they previously
        unfollowed, the existing record is reactivated.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        existing_follow = EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=True,
            notify_new_events=False,
        )
        original_id = existing_follow.id

        # Act
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                follow = follow_service.follow_event_series(
                    nonmember_user,
                    event_series,
                    notify_new_events=True,
                )

        # Assert
        assert follow.id == original_id
        assert follow.is_archived is False
        assert follow.notify_new_events is True

    def test_follow_event_series_already_following_raises_error(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
    ) -> None:
        """Test that following an already-followed series raises error.

        This test verifies that attempting to follow a series the user is
        already following results in an appropriate HTTP error.
        """
        # Arrange
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            with patch("events.service.follow_service.notification_requested.send"):
                follow_service.follow_event_series(nonmember_user, event_series)

        assert exc_info.value.status_code == 400
        assert "Already following" in str(exc_info.value)

    def test_follow_event_series_not_visible_raises_404(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
        organization: Organization,
    ) -> None:
        """Test that following an invisible series raises 404.

        This test verifies that users cannot follow event series they don't
        have visibility access to.
        """
        # Arrange - Keep series private
        organization.visibility = Organization.Visibility.PRIVATE
        organization.save()

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.follow_event_series(nonmember_user, event_series)

        assert exc_info.value.status_code == 404
        assert "Event series not found" in str(exc_info.value)


class TestUnfollowEventSeries:
    """Tests for unfollow_event_series function."""

    def test_unfollow_event_series_archives_follow(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that unfollowing archives the follow record.

        This test verifies that the follow record is soft-deleted (archived)
        rather than permanently deleted.
        """
        # Arrange
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
        )

        # Act
        follow_service.unfollow_event_series(nonmember_user, event_series)

        # Assert
        follow = EventSeriesFollow.objects.get(user=nonmember_user, event_series=event_series)
        assert follow.is_archived is True

    def test_unfollow_event_series_not_following_raises_error(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that unfollowing non-followed series raises error.

        This test verifies that users cannot unfollow a series they're not following.
        """
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.unfollow_event_series(nonmember_user, event_series)

        assert exc_info.value.status_code == 400
        assert "Not following" in str(exc_info.value)


class TestUpdateEventSeriesFollowPreferences:
    """Tests for update_event_series_follow_preferences function."""

    def test_update_preferences_success(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that preferences can be updated successfully.

        This test verifies that users can change their notification preferences
        for a series they're following.
        """
        # Arrange
        EventSeriesFollow.objects.create(
            user=nonmember_user,
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        )

        # Act
        follow = follow_service.update_event_series_follow_preferences(
            nonmember_user,
            event_series,
            notify_new_events=False,
        )

        # Assert
        assert follow.notify_new_events is False

    def test_update_preferences_not_following_raises_error(
        self,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that updating preferences for non-followed series raises error.

        This test verifies that users cannot update preferences for a series
        they're not following.
        """
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            follow_service.update_event_series_follow_preferences(
                nonmember_user,
                event_series,
                notify_new_events=False,
            )

        assert exc_info.value.status_code == 400
        assert "Not following" in str(exc_info.value)
