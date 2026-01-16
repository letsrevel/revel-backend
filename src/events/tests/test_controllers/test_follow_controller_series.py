"""Tests for event series follow controller endpoints.

This module tests the API endpoints for following event series
in the EventSeriesController.
"""

import typing as t
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import EventSeries, Organization
from events.models.follow import EventSeriesFollow

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


class TestEventSeriesFollowEndpoints:
    """Tests for event series follow endpoints in EventSeriesController."""

    class TestGetFollowStatus:
        """Tests for GET /{series_id}/follow endpoint."""

        def test_get_follow_status_when_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that follow status returns true with details when following.

            This test verifies the endpoint returns correct status for followed series.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
                notify_new_events=True,
            )

            # Act
            url = reverse("api:get_event_series_follow_status", kwargs={"series_id": event_series.id})
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["is_following"] is True
            assert data["follow"] is not None
            assert data["follow"]["notify_new_events"] is True

        def test_get_follow_status_when_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that follow status returns false when not following.

            This test verifies the endpoint correctly reports non-following status.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:get_event_series_follow_status", kwargs={"series_id": event_series.id})
            response = client.get(url)

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["is_following"] is False
            assert data["follow"] is None

        def test_get_follow_status_requires_auth(
            self,
            client: Client,
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that follow status endpoint requires authentication.

            This test verifies unauthenticated requests are rejected.
            """
            # Arrange
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:get_event_series_follow_status", kwargs={"series_id": event_series.id})
            response = client.get(url)

            # Assert
            assert response.status_code == 401

        def test_get_follow_status_series_not_found(
            self,
            public_user_client: tuple[Client, RevelUser],
        ) -> None:
            """Test that follow status returns 404 for non-existent series.

            This test verifies proper error handling for invalid series IDs.
            """
            # Arrange
            client, _ = public_user_client

            # Act
            url = reverse("api:get_event_series_follow_status", kwargs={"series_id": uuid4()})
            response = client.get(url)

            # Assert
            assert response.status_code == 404

    class TestFollowEventSeries:
        """Tests for POST /{series_id}/follow endpoint."""

        def test_follow_event_series_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
            django_capture_on_commit_callbacks: t.Any,
        ) -> None:
            """Test that following an event series creates a follow record.

            This test verifies the happy path of following an event series.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {"notify_new_events": True}

            # Act
            url = reverse("api:follow_event_series", kwargs={"series_id": event_series.id})
            with patch("events.service.follow_service.notification_requested.send"):
                with django_capture_on_commit_callbacks(execute=True):
                    response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["notify_new_events"] is True
            assert data["event_series"]["id"] == str(event_series.id)
            assert EventSeriesFollow.objects.filter(user=user, event_series=event_series, is_archived=False).exists()

        def test_follow_event_series_already_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that following an already-followed series returns 400.

            This test verifies duplicate follow attempts are rejected.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
            )
            payload = {"notify_new_events": True}

            # Act
            url = reverse("api:follow_event_series", kwargs={"series_id": event_series.id})
            with patch("events.service.follow_service.notification_requested.send"):
                response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 400
            assert "Already following" in response.json().get("detail", "")

        def test_follow_event_series_not_visible(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that following an invisible series returns 404.

            This test verifies users cannot follow series they can't view.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PRIVATE
            organization.save()
            payload = {"notify_new_events": True}

            # Act
            url = reverse("api:follow_event_series", kwargs={"series_id": event_series.id})
            response = client.post(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 404

    class TestUpdateEventSeriesFollow:
        """Tests for PATCH /{series_id}/follow endpoint."""

        def test_update_follow_preferences_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that notification preferences can be updated.

            This test verifies the happy path of updating follow preferences.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
                notify_new_events=True,
            )
            payload = {"notify_new_events": False}

            # Act
            url = reverse("api:update_event_series_follow", kwargs={"series_id": event_series.id})
            response = client.patch(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["notify_new_events"] is False

        def test_update_follow_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that updating preferences for non-followed series returns 400.

            This test verifies users cannot update preferences for series
            they're not following.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            payload = {"notify_new_events": False}

            # Act
            url = reverse("api:update_event_series_follow", kwargs={"series_id": event_series.id})
            response = client.patch(url, data=payload, content_type="application/json")

            # Assert
            assert response.status_code == 400
            assert "Not following" in response.json().get("detail", "")

    class TestUnfollowEventSeries:
        """Tests for DELETE /{series_id}/follow endpoint."""

        def test_unfollow_event_series_success(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that unfollowing a series archives the follow record.

            This test verifies the happy path of unfollowing an event series.
            """
            # Arrange
            client, user = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()
            EventSeriesFollow.objects.create(
                user=user,
                event_series=event_series,
                is_archived=False,
            )

            # Act
            url = reverse("api:unfollow_event_series", kwargs={"series_id": event_series.id})
            response = client.delete(url)

            # Assert
            assert response.status_code == 204
            follow = EventSeriesFollow.objects.get(user=user, event_series=event_series)
            assert follow.is_archived is True

        def test_unfollow_event_series_not_following(
            self,
            public_user_client: tuple[Client, RevelUser],
            event_series: EventSeries,
            organization: Organization,
        ) -> None:
            """Test that unfollowing non-followed series returns 400.

            This test verifies users cannot unfollow series they're not following.
            """
            # Arrange
            client, _ = public_user_client
            organization.visibility = Organization.Visibility.PUBLIC
            organization.save()

            # Act
            url = reverse("api:unfollow_event_series", kwargs={"series_id": event_series.id})
            response = client.delete(url)

            # Assert
            assert response.status_code == 400
            assert "Not following" in response.json().get("detail", "")
