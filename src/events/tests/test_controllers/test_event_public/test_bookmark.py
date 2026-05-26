"""Tests for the event bookmark endpoints, schema field, and dashboard facet."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventBookmark, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def user_client(revel_user_factory: t.Any) -> tuple[Client, RevelUser]:
    """Authenticated client for a user with no organization relationships."""
    user = revel_user_factory()
    refresh = RefreshToken.for_user(user)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
    return client, user


@pytest.fixture
def unlisted_event(organization: Organization) -> Event:
    """An unlisted event: hidden from discovery but reachable via direct link."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Unlisted Event",
        slug="unlisted-event",
        visibility=Event.Visibility.UNLISTED,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=start,
        end=start + timedelta(days=1),
    )


class TestBookmarkEndpoint:
    """Tests for POST/DELETE /events/{event_id}/bookmark."""

    def test_bookmark_creates_row(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """POST bookmark returns 201 and creates a bookmark."""
        client, user = user_client
        url = reverse("api:bookmark_event", kwargs={"event_id": str(public_event.id)})

        response = client.post(url, content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["event_id"] == str(public_event.id)
        assert EventBookmark.objects.filter(user=user, event=public_event).exists()

    def test_bookmark_is_idempotent(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """Bookmarking twice succeeds without duplicating: 201 then 200."""
        client, user = user_client
        url = reverse("api:bookmark_event", kwargs={"event_id": str(public_event.id)})

        first = client.post(url, content_type="application/json")
        second = client.post(url, content_type="application/json")

        assert first.status_code == 201  # created
        assert second.status_code == 200  # already existed
        assert second.json()["id"] == first.json()["id"]
        assert EventBookmark.objects.filter(user=user, event=public_event).count() == 1

    def test_unbookmark_removes_row(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """DELETE bookmark returns 204 and hard-deletes the bookmark."""
        client, user = user_client
        EventBookmark.objects.create(user=user, event=public_event)
        url = reverse("api:unbookmark_event", kwargs={"event_id": str(public_event.id)})

        response = client.delete(url)

        assert response.status_code == 204
        assert not EventBookmark.objects.filter(user=user, event=public_event).exists()

    def test_unbookmark_is_idempotent(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """DELETE bookmark on a non-bookmarked event still returns 204."""
        client, _ = user_client
        url = reverse("api:unbookmark_event", kwargs={"event_id": str(public_event.id)})

        response = client.delete(url)

        assert response.status_code == 204

    def test_bookmark_requires_authentication(self, public_event: Event) -> None:
        """Anonymous users cannot bookmark."""
        url = reverse("api:bookmark_event", kwargs={"event_id": str(public_event.id)})

        response = Client().post(url, content_type="application/json")

        assert response.status_code == 401

    def test_unbookmark_requires_authentication(self, public_event: Event) -> None:
        """Anonymous users cannot unbookmark (DELETE is a mutating endpoint too)."""
        url = reverse("api:unbookmark_event", kwargs={"event_id": str(public_event.id)})

        response = Client().delete(url)

        assert response.status_code == 401

    def test_cannot_bookmark_inaccessible_event(
        self, user_client: tuple[Client, RevelUser], private_event: Event
    ) -> None:
        """A user with no relationship to a private event cannot bookmark it (404)."""
        client, user = user_client
        url = reverse("api:bookmark_event", kwargs={"event_id": str(private_event.id)})

        response = client.post(url, content_type="application/json")

        assert response.status_code == 404
        assert not EventBookmark.objects.filter(user=user, event=private_event).exists()

    def test_can_bookmark_unlisted_event(self, user_client: tuple[Client, RevelUser], unlisted_event: Event) -> None:
        """Unlisted events are reachable via direct link, so they can be bookmarked."""
        client, user = user_client
        url = reverse("api:bookmark_event", kwargs={"event_id": str(unlisted_event.id)})

        response = client.post(url, content_type="application/json")

        assert response.status_code == 201
        assert EventBookmark.objects.filter(user=user, event=unlisted_event).exists()


class TestIsBookmarkedField:
    """Tests for the is_bookmarked field on event detail responses."""

    def test_detail_true_when_bookmarked(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """The detail endpoint reports is_bookmarked=true after bookmarking."""
        client, user = user_client
        EventBookmark.objects.create(user=user, event=public_event)
        url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

        response = client.get(url)

        assert response.status_code == 200
        assert response.json()["is_bookmarked"] is True

    def test_detail_false_when_not_bookmarked(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        """The detail endpoint reports is_bookmarked=false without a bookmark."""
        client, _ = user_client
        url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

        response = client.get(url)

        assert response.status_code == 200
        assert response.json()["is_bookmarked"] is False

    def test_detail_false_for_anonymous(self, public_event: Event) -> None:
        """Anonymous users always see is_bookmarked=false."""
        url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

        response = Client().get(url)

        assert response.status_code == 200
        assert response.json()["is_bookmarked"] is False


class TestDashboardBookmarkFacet:
    """Tests for the bookmarked facet on GET /dashboard/events."""

    def test_bookmarked_unlisted_event_appears_in_dashboard(
        self, user_client: tuple[Client, RevelUser], unlisted_event: Event
    ) -> None:
        """A bookmarked unlisted event surfaces on the dashboard via the bookmarked facet.

        This is the crux of issue #21: discovery hides unlisted events, but the dashboard
        gates on for_user() (which treats unlisted as publicly accessible), so bookmarked
        unlisted events remain findable.
        """
        client, user = user_client
        EventBookmark.objects.create(user=user, event=unlisted_event)
        url = reverse("api:dashboard_events")

        # Isolate the bookmarked facet: turn every other relationship off.
        response = client.get(
            url,
            {
                "owner": "false",
                "staff": "false",
                "member": "false",
                "rsvp_yes": "false",
                "rsvp_no": "false",
                "rsvp_maybe": "false",
                "got_ticket": "false",
                "got_invitation": "false",
                "bookmarked": "true",
            },
        )

        assert response.status_code == 200
        data = response.json()
        ids = {evt["id"] for evt in data["results"]}
        assert str(unlisted_event.id) in ids
        assert all(evt["is_bookmarked"] is True for evt in data["results"])

    def test_dashboard_excludes_bookmarks_when_facet_off(
        self, user_client: tuple[Client, RevelUser], unlisted_event: Event
    ) -> None:
        """With bookmarked=false and no other relationship, the dashboard is empty."""
        client, user = user_client
        EventBookmark.objects.create(user=user, event=unlisted_event)
        url = reverse("api:dashboard_events")

        response = client.get(
            url,
            {
                "owner": "false",
                "staff": "false",
                "member": "false",
                "rsvp_yes": "false",
                "rsvp_no": "false",
                "rsvp_maybe": "false",
                "got_ticket": "false",
                "got_invitation": "false",
                "bookmarked": "false",
            },
        )

        assert response.status_code == 200
        assert response.json()["count"] == 0
