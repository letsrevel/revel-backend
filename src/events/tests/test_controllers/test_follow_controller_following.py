"""Tests for FollowingController endpoints and end-to-end flow.

This module tests the /me/following endpoints and the complete
follow/unfollow lifecycle.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import EventSeries, Organization
from events.models.follow import EventSeriesFollow, OrganizationFollow

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


class TestFollowingController:
    """Tests for the FollowingController endpoints."""

    class TestListFollowedOrganizations:
        """Tests for GET /me/following/organizations endpoint."""

        def test_list_followed_organizations_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
            revel_user_factory: t.Any,
        ) -> None:
            """Test that followed organizations are returned correctly.

            This test verifies the paginated list returns all followed orgs
            with correct details.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Create second org to follow
            second_org = Organization.objects.create(
                name="Second Org",
                slug="second-org",
                owner=revel_user_factory(),
                visibility=Organization.Visibility.PUBLIC,
            )

            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
                notify_new_events=True,
            )
            OrganizationFollow.objects.create(
                user=user,
                organization=second_org,
                is_archived=False,
                notify_new_events=False,
            )

            # Act
            url = reverse("api:list_followed_organizations")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 2
            assert len(data["results"]) == 2

            # Verify org data is included
            org_slugs = {r["organization"]["slug"] for r in data["results"]}
            assert organization.slug in org_slugs
            assert second_org.slug in org_slugs

        def test_list_followed_organizations_excludes_archived(
            self,
            public_user_client: tuple[Client, RevelUser],
            organization: Organization,
            revel_user_factory: t.Any,
        ) -> None:
            """Test that archived follows are not included in the list.

            This test verifies that unfollowed (archived) organizations
            are excluded from the results.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            second_org = Organization.objects.create(
                name="Second Org",
                slug="second-org",
                owner=revel_user_factory(),
                visibility=Organization.Visibility.PUBLIC,
            )

            OrganizationFollow.objects.create(
                user=user,
                organization=organization,
                is_archived=False,
            )
            OrganizationFollow.objects.create(
                user=user,
                organization=second_org,
                is_archived=True,  # Archived
            )

            # Act
            url = reverse("api:list_followed_organizations")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1
            assert data["results"][0]["organization"]["slug"] == organization.slug

        def test_list_followed_organizations_empty(
            self,
            public_user_client: tuple[Client, RevelUser],
        ) -> None:
            """Test that empty list is returned when not following any orgs.

            This test verifies the endpoint handles users with no follows.
            """
            # Arrange
            client, _ = public_user_client

            # Act
            url = reverse("api:list_followed_organizations")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 0
            assert data["results"] == []

        def test_list_followed_organizations_requires_auth(
            self,
            client: Client,
        ) -> None:
            """Test that the endpoint requires authentication.

            This test verifies unauthenticated requests are rejected.
            """
            # Act
            url = reverse("api:list_followed_organizations")
            response = client.get(url)

            # Assert
            assert response.status_code == 401

    class TestListFollowedEventSeries:
        """Tests for GET /me/following/event-series endpoint."""

        def test_list_followed_event_series_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that followed event series are returned correctly.

            This test verifies the paginated list returns all followed series.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Create second series (inherits visibility from organization)
            second_series = EventSeries.objects.create(
                organization=organization,
                name="Second Series",
                slug="second-series",
            )

            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
            )
            EventSeriesFollow.objects.create(
                user=user,
                event_series=second_series,
                is_archived=False,
            )

            # Act
            url = reverse("api:list_followed_event_series")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 2
            assert len(data["results"]) == 2

        def test_list_followed_event_series_excludes_archived(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that archived follows are not included in the list.

            This test verifies unfollowed series are excluded.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            second_series = EventSeries.objects.create(
                organization=organization,
                name="Second Series",
                slug="second-series",
            )

            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
            )
            EventSeriesFollow.objects.create(
                user=user,
                event_series=second_series,
                is_archived=True,  # Archived
            )

            # Act
            url = reverse("api:list_followed_event_series")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 1

        def test_list_followed_event_series_empty(
            self,
            public_user_client: tuple[Client, RevelUser],
        ) -> None:
            """Test that empty list is returned when not following any series.

            This test verifies the endpoint handles users with no follows.
            """
            # Arrange
            client, _ = public_user_client

            # Act
            url = reverse("api:list_followed_event_series")
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 0
            assert data["results"] == []

        def test_list_followed_event_series_requires_auth(
            self,
            client: Client,
        ) -> None:
            """Test that the endpoint requires authentication.

            This test verifies unauthenticated requests are rejected.
            """
            # Act
            url = reverse("api:list_followed_event_series")
            response = client.get(url)

            # Assert
            assert response.status_code == 401


class TestFollowEndToEndFlow:
    """End-to-end tests for the complete follow/unfollow flow."""

    def test_complete_organization_follow_flow(
        self,
        public_user_client: tuple[Client, RevelUser],
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test the complete flow of following, updating, and unfollowing an org.

        This test verifies the entire lifecycle of an organization follow:
        1. Follow the organization
        2. Verify it appears in the followed list
        3. Update notification preferences
        4. Unfollow the organization
        5. Re-follow (reactivation)
        """
        client, user = public_user_client
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # 1. Follow the organization
        follow_url = reverse("api:follow_organization", kwargs={"slug": organization.slug})
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    follow_url,
                    data={"notify_new_events": True, "notify_announcements": True},
                    content_type="application/json",
                )
        assert response.status_code == 201

        # 2. Verify in followed list
        list_url = reverse("api:list_followed_organizations")
        response = client.get(list_url)
        assert response.status_code == 200
        assert response.json()["count"] == 1

        # 3. Update preferences
        update_url = reverse("api:update_organization_follow", kwargs={"slug": organization.slug})
        response = client.patch(
            update_url,
            data={"notify_new_events": False},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["notify_new_events"] is False

        # 4. Unfollow
        unfollow_url = reverse("api:unfollow_organization", kwargs={"slug": organization.slug})
        response = client.delete(unfollow_url)
        assert response.status_code == 204

        # Verify no longer in list
        response = client.get(list_url)
        assert response.json()["count"] == 0

        # 5. Re-follow (reactivation)
        with patch("events.service.follow_service.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    follow_url,
                    data={"notify_new_events": True, "notify_announcements": True},
                    content_type="application/json",
                )
        assert response.status_code == 201

        # Verify same record was reactivated (only 1 record exists)
        assert OrganizationFollow.objects.filter(user=user, organization=organization).count() == 1
        assert OrganizationFollow.objects.filter(user=user, organization=organization, is_archived=False).count() == 1
