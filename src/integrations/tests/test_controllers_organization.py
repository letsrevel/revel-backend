"""Org-level integration endpoints: owner-only, connect/select/update/disconnect via FakeProvider."""

import orjson
import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization
from integrations.models import PlatformConnection
from integrations.providers.base import RemoteAccount
from integrations.service import connection_service
from integrations.service.state import CONNECT_STATE_COOKIE
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


def _connect(client: Client, organization: Organization) -> str:
    url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "fake"})
    response = client.post(url, content_type="application/json")
    assert response.status_code == 200, response.content
    return str(response.json()["authorize_url"])


def test_list_shows_enabled_provider_unconnected(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    url = reverse("api:list_integrations", kwargs={"slug": organization.slug})
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    assert response.json() == [
        {
            "provider": "fake",
            "display_name": "Fake",
            "status": None,
            "remote_account_name": "",
            "auto_sync": False,
            "last_error": None,
            "connected_at": None,
        }
    ]


def test_stranger_cannot_list(
    organization: Organization, fake_provider: FakeProvider, django_user_model: type[RevelUser]
) -> None:
    """A user with no relationship to the organization gets 404, not 403.

    Unlike staff (who are members and so surface via ``Organization.for_user`` and then get
    rejected by ``IsOrganizationOwner`` with 403), a stranger to a non-public organization
    never sees it in their visible queryset at all, so ``get_object_or_exception`` 404s before
    the owner-permission check ever runs.
    """
    stranger = django_user_model.objects.create_user(username="int_stranger", email="stranger@example.com")
    refresh = RefreshToken.for_user(stranger)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
    url = reverse("api:list_integrations", kwargs={"slug": organization.slug})
    assert client.get(url).status_code == 404


def test_staff_cannot_list_or_connect(
    organization_staff_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    assert (
        organization_staff_client.get(reverse("api:list_integrations", kwargs={"slug": organization.slug})).status_code
        == 403
    )
    url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "fake"})
    assert organization_staff_client.post(url, content_type="application/json").status_code == 403


def test_connect_returns_url_and_sets_state_cookie(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "fake"})
    response = organization_owner_client.post(url, content_type="application/json")
    assert response.status_code == 200
    cookie = response.cookies[CONNECT_STATE_COOKIE]
    assert cookie.value and cookie.value in response.json()["authorize_url"]
    assert cookie["httponly"] and cookie["path"] == "/api/integrations"
    assert cookie["samesite"] == "Lax"
    # pytest-django's test environment forces settings.DEBUG = False regardless of .env, so the
    # controller's secure=not DEBUG is truthy here (unlike a DEBUG=True dev server).
    assert not settings.DEBUG
    assert cookie["secure"]


def test_connect_unknown_provider_404(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "nope"})
    response = organization_owner_client.post(url, content_type="application/json")
    assert response.status_code == 404
    assert response.json()["code"] == "provider_unknown"


def test_connect_twice_409(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    connection_service.complete_connect(start.state, "c")
    url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "fake"})
    response = organization_owner_client.post(url, content_type="application/json")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "This platform is already connected.",
        "code": "already_connected",
        "provider_message": None,
    }


def test_accounts_and_select(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    fake_provider.accounts = [RemoteAccount(remote_id="a", name="A"), RemoteAccount(remote_id="b", name="B")]
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    connection_service.complete_connect(start.state, "c")
    accounts_url = reverse("api:integration_accounts", kwargs={"slug": organization.slug, "provider": "fake"})
    assert organization_owner_client.get(accounts_url).json() == [
        {"remote_id": "a", "name": "A"},
        {"remote_id": "b", "name": "B"},
    ]
    select_url = reverse("api:integration_select_account", kwargs={"slug": organization.slug, "provider": "fake"})
    response = organization_owner_client.post(
        select_url, data=orjson.dumps({"remote_id": "b"}), content_type="application/json"
    )
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["remote_account_name"]) == ("active", "B")


def test_update_auto_sync_and_disconnect(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    connection_service.complete_connect(start.state, "c")
    url = reverse("api:integration_update", kwargs={"slug": organization.slug, "provider": "fake"})
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"auto_sync": True}), content_type="application/json"
    )
    assert response.status_code == 200 and response.json()["auto_sync"] is True
    response = organization_owner_client.delete(
        reverse("api:integration_disconnect", kwargs={"slug": organization.slug, "provider": "fake"})
    )
    assert response.status_code == 200
    assert not PlatformConnection.objects.exists()


def test_update_when_not_connected_404(
    organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
) -> None:
    url = reverse("api:integration_update", kwargs={"slug": organization.slug, "provider": "fake"})
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"auto_sync": True}), content_type="application/json"
    )
    assert response.status_code == 404 and response.json()["code"] == "provider_not_connected"
