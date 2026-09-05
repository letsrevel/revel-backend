"""Event-level integration endpoints: manage-event staff allowed, member/stranger not; push is 202 + dispatch."""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, TicketTier
from integrations.models import EventLink, PlatformConnection
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def clean_event(event: Event) -> Event:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return event


def _url(name: str, event: Event, provider: str | None = None) -> str:
    kwargs: dict[str, object] = {"event_id": event.id}
    if provider:
        kwargs["provider"] = provider
    return reverse(f"api:{name}", kwargs=kwargs)


def test_list_empty_then_after_push(  # type: ignore[no-untyped-def]
    organization_owner_client: Client,
    clean_event: Event,
    connected: PlatformConnection,
    django_capture_on_commit_callbacks,
) -> None:
    assert organization_owner_client.get(_url("list_event_integrations", clean_event)).json() == []
    with django_capture_on_commit_callbacks(execute=True):
        response = organization_owner_client.post(
            _url("event_integration_push", clean_event, "fake"), content_type="application/json"
        )
    assert response.status_code == 202, response.content
    assert response.json()["sync_state"] in ("pending", "in_sync")
    rows = organization_owner_client.get(_url("list_event_integrations", clean_event)).json()
    assert rows[0]["remote_id"] == "ev-1" and rows[0]["remote_status"] == "draft"


def test_staff_with_manage_event_can_push(
    organization_staff_client: Client, clean_event: Event, connected: PlatformConnection
) -> None:
    response = organization_staff_client.post(
        _url("event_integration_push", clean_event, "fake"), content_type="application/json"
    )
    assert response.status_code == 202, response.content


def test_stranger_cannot_see(clean_event: Event, connected: PlatformConnection, django_user_model) -> None:  # type: ignore[no-untyped-def]
    from ninja_jwt.tokens import RefreshToken

    user = django_user_model.objects.create_user(username="nobody", email="nobody@example.com", password="p")
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]
    assert client.get(_url("list_event_integrations", clean_event)).status_code in (403, 404)


def test_push_ineligible_400_with_code(
    organization_owner_client: Client, clean_event: Event, connected: PlatformConnection
) -> None:
    clean_event.event_type = Event.EventType.PRIVATE
    clean_event.save()
    response = organization_owner_client.post(
        _url("event_integration_push", clean_event, "fake"), content_type="application/json"
    )
    assert response.status_code == 400 and response.json()["code"] == "event_private"


def test_publish_and_patch(
    organization_owner_client: Client, clean_event: Event, connected: PlatformConnection
) -> None:
    sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    response = organization_owner_client.post(
        _url("event_integration_publish", clean_event, "fake"), content_type="application/json"
    )
    assert response.status_code == 200 and response.json()["remote_status"] == "live"
    response = organization_owner_client.patch(
        _url("event_integration_update", clean_event, "fake"),
        data=orjson.dumps({"auto_sync": True}),
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["effective_auto_sync"] is True
    assert EventLink.objects.get().auto_sync is True
