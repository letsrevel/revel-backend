"""The follow API's ``notify_announcements`` is a facade over the per-org mute (#1031).

``OrganizationFollow.notify_announcements`` is no longer read: the response reports
``not muted`` and writes go to ``NotificationPreference.muted_organizations``.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Organization
from events.models.follow import OrganizationFollow

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="facade_user")


@pytest.fixture
def client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


@pytest.fixture
def org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="facade_owner")
    return Organization.objects.create(
        name="Facade Org", slug="facade-org", owner=owner, visibility=Organization.Visibility.PUBLIC
    )


def _is_muted(user: RevelUser, org: Organization) -> bool:
    return user.notification_preferences.muted_organizations.filter(pk=org.pk).exists()


def _follow(client: Client, org: Organization, payload: dict[str, t.Any]) -> t.Any:
    with patch("notifications.signals.notification_requested.send"):
        return client.post(
            reverse("api:follow_organization", kwargs={"slug": org.slug}),
            data=payload,
            content_type="application/json",
        )


def _patch(client: Client, org: Organization, payload: dict[str, t.Any]) -> t.Any:
    return client.patch(
        reverse("api:update_organization_follow", kwargs={"slug": org.slug}),
        data=payload,
        content_type="application/json",
    )


class TestFollowCreate:
    def test_create_with_false_mutes(self, client: Client, user: RevelUser, org: Organization) -> None:
        response = _follow(client, org, {"notify_announcements": False})

        assert response.status_code == 201
        assert response.json()["notify_announcements"] is False
        assert _is_muted(user, org)

    def test_create_default_does_not_remove_existing_mute(
        self, client: Client, user: RevelUser, org: Organization
    ) -> None:
        user.notification_preferences.muted_organizations.add(org)

        response = _follow(client, org, {"notify_announcements": True})

        assert response.status_code == 201
        assert response.json()["notify_announcements"] is False
        assert _is_muted(user, org)

    def test_create_default_unmuted(self, client: Client, user: RevelUser, org: Organization) -> None:
        response = _follow(client, org, {})

        assert response.json()["notify_announcements"] is True
        assert not _is_muted(user, org)


class TestFollowUpdate:
    def test_round_trip(self, client: Client, user: RevelUser, org: Organization) -> None:
        _follow(client, org, {})

        muted = _patch(client, org, {"notify_announcements": False})
        assert muted.status_code == 200
        assert muted.json()["notify_announcements"] is False
        assert _is_muted(user, org)

        unmuted = _patch(client, org, {"notify_announcements": True})
        assert unmuted.json()["notify_announcements"] is True
        assert not _is_muted(user, org)

    def test_update_without_field_leaves_mute_alone(self, client: Client, user: RevelUser, org: Organization) -> None:
        _follow(client, org, {})
        user.notification_preferences.muted_organizations.add(org)

        response = _patch(client, org, {"notify_new_events": False})

        assert response.json()["notify_announcements"] is False
        assert _is_muted(user, org)

    def test_mute_via_preferences_api_is_reflected_in_follow_status(self, client: Client, org: Organization) -> None:
        _follow(client, org, {})
        client.put(f"/api/notification-preferences/muted-organizations/{org.id}")

        response = client.get(reverse("api:get_organization_follow_status", kwargs={"slug": org.slug}))

        assert response.json()["follow"]["notify_announcements"] is False

    def test_stale_column_is_not_read(self, client: Client, user: RevelUser, org: Organization) -> None:
        """A legacy ``notify_announcements=False`` row without a mute reports True (the mute is the truth)."""
        OrganizationFollow.objects.create(user=user, organization=org, notify_announcements=False)

        response = client.get(reverse("api:get_organization_follow_status", kwargs={"slug": org.slug}))

        assert response.json()["follow"]["notify_announcements"] is True


class TestFollowedOrganizationsList:
    def _make_follows(self, user: RevelUser, owner: RevelUser, count: int, start: int) -> None:
        for i in range(start, start + count):
            org = Organization.objects.create(name=f"Listed {i}", slug=f"listed-{i}", owner=owner)
            OrganizationFollow.objects.create(user=user, organization=org)
            if i % 2:
                user.notification_preferences.muted_organizations.add(org)

    def test_list_reports_mute_state(self, client: Client, user: RevelUser, org: Organization) -> None:
        self._make_follows(user, org.owner, 4, start=0)

        response = client.get(reverse("api:list_followed_organizations"))

        assert response.status_code == 200
        by_slug = {item["organization"]["slug"]: item["notify_announcements"] for item in response.json()["results"]}
        assert by_slug == {"listed-0": True, "listed-1": False, "listed-2": True, "listed-3": False}

    def test_list_has_no_n_plus_one(
        self,
        client: Client,
        user: RevelUser,
        org: Organization,
        django_assert_max_num_queries: t.Any,
        django_assert_num_queries: t.Any,
    ) -> None:
        url = reverse("api:list_followed_organizations")
        self._make_follows(user, org.owner, 1, start=0)
        with django_assert_max_num_queries(50) as one:
            client.get(url)

        self._make_follows(user, org.owner, 6, start=1)
        with django_assert_num_queries(len(one.captured_queries)):
            response = client.get(url)

        assert len(response.json()["results"]) == 7
