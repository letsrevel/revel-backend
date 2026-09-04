"""Org-level integration endpoints: owner-only, connect/select/update/disconnect via FakeProvider."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

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
