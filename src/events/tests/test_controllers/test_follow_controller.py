"""Tests for organization follow controller endpoints.

This module tests the API endpoints for following organizations
in the OrganizationController.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization
from events.models.follow import OrganizationFollow

pytestmark = pytest.mark.django_db


@pytest.fixture
def public_user_client(revel_user_factory: t.Any) -> tuple[Client, RevelUser]:
    """API client for a user with no organization relationships.

    Returns:
        Tuple of (authenticated Client, RevelUser instance)
    """
    user = revel_user_factory()
    refresh = RefreshToken.for_user(user)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
    return client, user


class TestOrganizationFollowEndpoints:
    """Tests for organization follow endpoints in OrganizationController."""

    class TestGetFollowStatus:
        """Tests for GET /{slug}/follow endpoint."""

        def test_get_follow_status_when_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that follow status returns true with follow details when following.

            This test verifies that the endpoint returns the correct follow status
            and includes the follow object with notification preferences.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
                notify_new_events=True,
                notify_announcements=False,
            )

            # Act
            url = reverse("api:get_organization_follow_status", kwargs={"slug": organization.slug})
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["is_following"] is True
            assert data["follow"] is not None
            assert data["follow"]["notify_new_events"] is True
            assert data["follow"]["notify_announcements"] is False

        def test_get_follow_status_when_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that follow status returns false when not following.

            This test verifies that the endpoint correctly reports non-following status.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:get_organization_follow_status", kwargs={"slug": organization.slug})
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["is_following"] is False
            assert data["follow"] is None

        def test_get_follow_status_requires_auth(
            self,
            client: Client,
            organization: Organization,
        ) -> None:
            """Test that follow status endpoint requires authentication.

            This test verifies that unauthenticated requests are rejected.
            """
            # Arrange
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:get_organization_follow_status", kwargs={"slug": organization.slug})
            response = client.get(url)

            # Assert
            assert response.status_code == 401

        def test_get_follow_status_org_not_found(
            self,
            public_user_client: tuple[Client, RevelUser],
        ) -> None:
            """Test that follow status returns 404 for non-existent org.

            This test verifies that the endpoint returns proper error for invalid slugs.
            """
            # Arrange
            client, _ = public_user_client

            # Act
            url = reverse("api:get_organization_follow_status", kwargs={"slug": "non-existent"})
            response = client.get(url)

            # Assert
            assert response.status_code == 404

    class TestFollowOrganization:
        """Tests for POST /{slug}/follow endpoint."""

        def test_follow_organization_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
            django_capture_on_commit_callbacks: t.Any,
        ) -> None:
            """Test that following an organization creates a follow record.

            This test verifies the happy path of following an organization
            with default notification preferences.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {
                "notify_new_events": True,
                "notify_announcements": True,
            }

            # Act
            url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
            with patch("events.service.follow_service.notification_requested.send"):
                with django_capture_on_commit_callbacks(execute=True):
                    response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["notify_new_events"] is True
            assert data["notify_announcements"] is True
            assert data["organization"]["slug"] == organization.slug
            assert OrganizationFollow.objects.filter(user=user, organization=organization, is_archived=False).exists()

        def test_follow_organization_with_custom_preferences(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
            django_capture_on_commit_callbacks: t.Any,
        ) -> None:
            """Test that custom notification preferences are saved.

            This test verifies that users can opt out of specific notifications.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {
                "notify_new_events": False,
                "notify_announcements": True,
            }

            # Act
            url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
            with patch("events.service.follow_service.notification_requested.send"):
                with django_capture_on_commit_callbacks(execute=True):
                    response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["notify_new_events"] is False
            assert data["notify_announcements"] is True

        def test_follow_organization_already_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that following an already-followed org returns 400.

            This test verifies that duplicate follow attempts are rejected.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
            )
            payload = {"notify_new_events": True, "notify_announcements": True}

            # Act
            url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
            with patch("events.service.follow_service.notification_requested.send"):
                response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 400
            assert "Already following" in response.json().get("detail", "")

        def test_follow_organization_not_visible(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that following an invisible org returns 404.

            This test verifies that users cannot follow organizations they
            don't have access to view.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PRIVATE
            organization.save()
            payload = {"notify_new_events": True, "notify_announcements": True}

            # Act
            url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
            response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 404

        def test_follow_organization_requires_auth(
            self,
            client: Client,
            organization: Organization,
        ) -> None:
            """Test that follow endpoint requires authentication.

            This test verifies unauthenticated requests are rejected.
            """
            # Arrange
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {"notify_new_events": True, "notify_announcements": True}

            # Act
            url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
            response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 401

    class TestUpdateOrganizationFollow:
        """Tests for PATCH /{slug}/follow endpoint."""

        def test_update_follow_preferences_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that notification preferences can be updated.

            This test verifies the happy path of updating follow preferences.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
                notify_new_events=True,
                notify_announcements=True,
            )
            payload = {"notify_new_events": False}

            # Act
            url = reverse("api:update_organization_follow", kwargs={"slug": organization.slug})
            response = client.patch(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["notify_new_events"] is False
            assert data["notify_announcements"] is True  # Unchanged

        def test_update_follow_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that updating preferences for non-followed org returns 400.

            This test verifies that users cannot update preferences for organizations
            they're not following.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {"notify_new_events": False}

            # Act
            url = reverse("api:update_organization_follow", kwargs={"slug": organization.slug})
            response = client.patch(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 400
            assert "Not following" in response.json().get("detail", "")

    class TestUnfollowOrganization:
        """Tests for DELETE /{slug}/follow endpoint."""

        def test_unfollow_organization_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that unfollowing an organization archives the follow.

            This test verifies the happy path of unfollowing an organization.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
            )

            # Act
            url = reverse("api:unfollow_organization", kwargs={"slug": organization.slug})
            response = client.delete(url)

            # Assert
            assert response.status_code == 204
            follow = OrganizationFollow.objects.get(user=user, organization=organization)
            assert follow.is_archived is True

        def test_unfollow_organization_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
        ) -> None:
            """Test that unfollowing non-followed org returns 400.

            This test verifies that users cannot unfollow organizations
            they're not currently following.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:unfollow_organization", kwargs={"slug": organization.slug})
            response = client.delete(url)

            # Assert
            assert response.status_code == 400
            assert "Not following" in response.json().get("detail", "")
