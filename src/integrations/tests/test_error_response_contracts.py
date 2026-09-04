"""Wire-contract tests for the ``IntegrationError`` response body (spec §9).

Companion to ``events/tests/test_controllers/test_error_response_contracts.py``: same
purpose (pin the actual wire shape so declared schemas stay honest), scoped to the
``integrations`` app because its fixtures (``organization_owner_client``,
``organization``, ``fake_provider``) live in ``integrations/tests/conftest.py`` and
are not visible from the events test directory.
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Organization
from integrations.service import connection_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


def assert_integration_error_body(response: t.Any, status_code: int, code: str) -> dict[str, t.Any]:
    """Assert ``status_code`` with a body that is exactly ``{"detail", "code", "provider_message"}``."""
    assert response.status_code == status_code, response.content
    body = response.json()
    assert set(body.keys()) == {"detail", "code", "provider_message"}, body
    assert isinstance(body["detail"], str) and body["detail"], body
    assert body["code"] == code, body
    return t.cast(dict[str, t.Any], body)


class TestIntegrationConnectErrorContracts:
    """``connect`` (spec §6) raises ``IntegrationError`` for unknown providers and re-connects."""

    def test_unknown_provider_returns_404_contract(
        self, organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
    ) -> None:
        url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "nope"})
        response = organization_owner_client.post(url, content_type="application/json")
        assert_integration_error_body(response, 404, "provider_unknown")

    def test_already_connected_returns_409_contract(
        self, organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
    ) -> None:
        start = connection_service.begin_connect(organization, organization.owner, "fake")
        connection_service.complete_connect(start.state, "c")
        url = reverse("api:integration_connect", kwargs={"slug": organization.slug, "provider": "fake"})
        response = organization_owner_client.post(url, content_type="application/json")
        assert_integration_error_body(response, 409, "already_connected")


class TestIntegrationUpdateErrorContracts:
    """``update`` (auto-sync toggle) requires an existing connection."""

    def test_not_connected_returns_404_contract(
        self, organization_owner_client: Client, organization: Organization, fake_provider: FakeProvider
    ) -> None:
        url = reverse("api:integration_update", kwargs={"slug": organization.slug, "provider": "fake"})
        response = organization_owner_client.patch(url, data=b'{"auto_sync": true}', content_type="application/json")
        assert_integration_error_body(response, 404, "provider_not_connected")


class TestIntegrationWebhookErrorContracts:
    """``webhook`` (public, secret-authenticated receiver) — unknown secret vs. malformed body."""

    def test_unknown_secret_returns_404(self, organization: Organization, fake_provider: FakeProvider) -> None:
        """An unrecognized secret is a plain ``Http404`` — the app-agnostic ``{"detail": ...}`` shape."""
        start = connection_service.begin_connect(organization, organization.owner, "fake")
        connection_service.complete_connect(start.state, "c")
        url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": "wrong"})
        response = Client().post(url, data=b"{}", content_type="application/json")
        assert response.status_code == 404, response.content
        body = response.json()
        assert isinstance(body.get("detail"), str) and body["detail"]
        assert "code" not in body, body

    def test_malformed_body_returns_400_contract(self, organization: Organization, fake_provider: FakeProvider) -> None:
        start = connection_service.begin_connect(organization, organization.owner, "fake")
        conn = connection_service.complete_connect(start.state, "c")
        url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": conn.webhook_secret})
        response = Client().post(url, data=b"{}", content_type="application/json")
        assert_integration_error_body(response, 400, "provider_rejected")
