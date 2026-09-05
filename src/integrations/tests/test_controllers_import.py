"""Owner-only remote listing and import endpoints."""

from datetime import UTC, datetime, timedelta

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from integrations.models import PlatformConnection
from integrations.providers.base import RemoteEvent
from integrations.service import connection_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, "c")
    s = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
    fake_provider.create_event(
        conn.token(),
        "acc-1",
        RemoteEvent(name="R", start=s, end=s + timedelta(hours=1), timezone="UTC", currency="EUR"),
    )
    return conn


def test_remote_events_and_import_flow(  # type: ignore[no-untyped-def]
    organization_owner_client: Client, organization, connected: PlatformConnection, django_capture_on_commit_callbacks
) -> None:
    url = reverse("api:integration_remote_events", kwargs={"slug": organization.slug, "provider": "fake"})
    rows = organization_owner_client.get(url).json()
    assert rows[0]["remote_id"] == "ev-1" and rows[0]["already_linked"] is False
    url = reverse("api:integration_import", kwargs={"slug": organization.slug, "provider": "fake"})
    with django_capture_on_commit_callbacks(execute=True):
        response = organization_owner_client.post(
            url, data=orjson.dumps({"remote_ids": ["ev-1"]}), content_type="application/json"
        )
    assert response.status_code == 202 and response.json() == {"queued": ["ev-1"], "skipped": []}


def test_staff_forbidden(organization_staff_client: Client, organization, connected: PlatformConnection) -> None:  # type: ignore[no-untyped-def]
    url = reverse("api:integration_remote_events", kwargs={"slug": organization.slug, "provider": "fake"})
    assert organization_staff_client.get(url).status_code == 403


def test_import_validation(organization_owner_client: Client, organization, connected: PlatformConnection) -> None:  # type: ignore[no-untyped-def]
    url = reverse("api:integration_import", kwargs={"slug": organization.slug, "provider": "fake"})
    assert (
        organization_owner_client.post(
            url, data=orjson.dumps({"remote_ids": []}), content_type="application/json"
        ).status_code
        == 422
    )
