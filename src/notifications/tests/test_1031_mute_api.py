"""Per-organization announcement mute API on notification preferences (#1031)."""

import uuid

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization

pytestmark = pytest.mark.django_db


def _url(organization_id: uuid.UUID) -> str:
    return f"/api/notification-preferences/muted-organizations/{organization_id}"


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


@pytest.fixture
def private_org(django_user_model: type[RevelUser]) -> Organization:
    """A private organization the test user is NOT a member of (e.g. they only hold a ticket)."""
    owner = django_user_model.objects.create_user(username="mute-owner", email="mute-owner@example.com")
    return Organization.objects.create(
        name="Private Org", slug="private-mute-org", owner=owner, visibility=Organization.Visibility.PRIVATE
    )


class TestMuteEndpoints:
    def test_get_preferences_exposes_empty_muted_ids(self, regular_user: RevelUser) -> None:
        response = _client(regular_user).get("/api/notification-preferences")

        assert response.status_code == 200
        assert response.json()["muted_organization_ids"] == []

    def test_put_mutes_and_is_idempotent(self, regular_user: RevelUser, organization: Organization) -> None:
        client = _client(regular_user)

        first = client.put(_url(organization.id))
        second = client.put(_url(organization.id))

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["muted_organization_ids"] == [str(organization.id)]
        assert list(regular_user.notification_preferences.muted_organizations.all()) == [organization]

    def test_delete_unmutes_and_is_idempotent(self, regular_user: RevelUser, organization: Organization) -> None:
        regular_user.notification_preferences.muted_organizations.add(organization)
        client = _client(regular_user)

        first = client.delete(_url(organization.id))
        second = client.delete(_url(organization.id))

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["muted_organization_ids"] == []
        assert not regular_user.notification_preferences.muted_organizations.exists()

    def test_attendee_of_private_org_can_mute(self, regular_user: RevelUser, private_org: Organization) -> None:
        """Visibility is not required: event attendees of private orgs receive their announcements too."""
        response = _client(regular_user).put(_url(private_org.id))

        assert response.status_code == 200
        assert response.json()["muted_organization_ids"] == [str(private_org.id)]

    @pytest.mark.parametrize("method", ["put", "delete"])
    def test_unknown_org_is_404(self, regular_user: RevelUser, method: str) -> None:
        response = getattr(_client(regular_user), method)(_url(uuid.uuid4()))

        assert response.status_code == 404
        assert not regular_user.notification_preferences.muted_organizations.exists()

    @pytest.mark.parametrize("method", ["put", "delete"])
    def test_requires_auth(self, organization: Organization, method: str) -> None:
        response = getattr(Client(), method)(_url(organization.id))

        assert response.status_code == 401

    def test_creates_preferences_if_missing(self, regular_user: RevelUser, organization: Organization) -> None:
        regular_user.notification_preferences.delete()

        response = _client(regular_user).put(_url(organization.id))

        assert response.status_code == 200
        assert response.json()["muted_organization_ids"] == [str(organization.id)]

    def test_mute_only_affects_own_preferences(
        self, regular_user: RevelUser, user: RevelUser, organization: Organization
    ) -> None:
        _client(regular_user).put(_url(organization.id))

        assert not user.notification_preferences.muted_organizations.exists()
