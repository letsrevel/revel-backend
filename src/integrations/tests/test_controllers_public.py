"""OAuth callback (redirects, never JSON) and the webhook receiver (records only in phase 1)."""

import typing as t

import orjson
import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test.client import Client
from django.urls import reverse

from events.models import Organization
from integrations.models import PlatformConnection, WebhookDelivery
from integrations.service import connection_service
from integrations.service.state import CONNECT_STATE_COOKIE
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


def _callback(client: Client, provider: str, **params: str) -> HttpResponse:
    return t.cast(HttpResponse, client.get(reverse("api:integration_callback", kwargs={"provider": provider}), params))


def test_callback_success_redirects_connected(organization: Organization, fake_provider: FakeProvider) -> None:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    client = Client()
    client.cookies[CONNECT_STATE_COOKIE] = start.state
    response = _callback(client, "fake", code="c1", state=start.state)
    assert response.status_code == 302
    assert (
        response["Location"]
        == f"{settings.FRONTEND_BASE_URL}/org/{organization.slug}/settings/integrations?connected=fake"
    )
    assert PlatformConnection.objects.get().status == "active"
    assert CONNECT_STATE_COOKIE in response.cookies and response.cookies[CONNECT_STATE_COOKIE]["max-age"] == 0


def test_callback_multi_account_redirects_select(organization: Organization, fake_provider: FakeProvider) -> None:
    from integrations.providers.base import RemoteAccount

    fake_provider.accounts = [RemoteAccount(remote_id="a", name="A"), RemoteAccount(remote_id="b", name="B")]
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    client = Client()
    client.cookies[CONNECT_STATE_COOKIE] = start.state
    response = _callback(client, "fake", code="c1", state=start.state)
    assert response["Location"].endswith("?select=fake")


def test_callback_without_cookie_redirects_error(organization: Organization, fake_provider: FakeProvider) -> None:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    response = _callback(Client(), "fake", code="c1", state=start.state)
    assert response.status_code == 302
    assert response["Location"].endswith("?error=state_invalid")
    assert not PlatformConnection.objects.exists()


def test_callback_denied_by_user_redirects_error(organization: Organization, fake_provider: FakeProvider) -> None:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    client = Client()
    client.cookies[CONNECT_STATE_COOKIE] = start.state
    response = _callback(client, "fake", error="access_denied", state=start.state)
    assert response["Location"].endswith("?error=provider_rejected")


def test_callback_garbage_state_redirects_generic_error(fake_provider: FakeProvider) -> None:
    """No valid state → no org slug is known → generic landing page with the error code."""
    response = _callback(Client(), "nope", code="c", state="s")
    assert response.status_code == 302
    assert response["Location"] == f"{settings.FRONTEND_BASE_URL}/org?error=state_invalid"


def _connected(organization: Organization) -> PlatformConnection:
    start = connection_service.begin_connect(organization, organization.owner, "fake")
    return connection_service.complete_connect(start.state, "c")


def test_webhook_records_delivery(organization: Organization, fake_provider: FakeProvider) -> None:
    conn = _connected(organization)
    url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": conn.webhook_secret})
    response = Client().post(
        url, data=orjson.dumps({"action": "order.placed", "path": "/orders/1/"}), content_type="application/json"
    )
    assert response.status_code == 200
    delivery = WebhookDelivery.objects.get()
    assert (delivery.connection_id, delivery.action, delivery.resource_path, delivery.outcome) == (
        conn.id,
        "order.placed",
        "/orders/1/",
        "received",
    )


def test_webhook_unknown_secret_404(organization: Organization, fake_provider: FakeProvider) -> None:
    _connected(organization)
    url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": "wrong"})
    assert Client().post(url, data=b"{}", content_type="application/json").status_code == 404
    assert not WebhookDelivery.objects.exists()


def test_webhook_malformed_400(organization: Organization, fake_provider: FakeProvider) -> None:
    conn = _connected(organization)
    url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": conn.webhook_secret})
    response = Client().post(url, data=b"{}", content_type="application/json")
    assert response.status_code == 400 and response.json()["code"] == "provider_rejected"
