"""Deliveries are dispatched on commit; kinds route to counts or the status matrix; unknown → ignored."""

import typing as t
from decimal import Decimal

import orjson
import pytest
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse

from events.models import Event, TicketTier
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, WebhookDelivery
from integrations.service import connection_service, sync_service, webhook_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def pushed(event: Event, connected: PlatformConnection) -> EventLink:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return sync_service.push_link(sync_service.ensure_link(event, connected))


def _deliver(conn: PlatformConnection, action: str, path: str, **raw: object) -> WebhookDelivery:
    return WebhookDelivery.objects.create(
        connection=conn, action=action, resource_path=path, payload={"action": action, "path": path, **raw}
    )


def test_webhook_endpoint_dispatches_on_commit(
    connected: PlatformConnection,
    pushed: EventLink,
    fake_provider: FakeProvider,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": connected.webhook_secret})
    for c in fake_provider.events[pushed.remote_id].ticket_classes:
        c.quantity_sold = 4
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = Client().post(
            url,
            data=orjson.dumps({"action": "order.placed", "path": f"/events/{pushed.remote_id}/"}),
            content_type="application/json",
        )
    assert response.status_code == 200 and len(callbacks) == 1
    delivery = WebhookDelivery.objects.get()
    assert delivery.outcome == WebhookDelivery.Outcome.PROCESSED
    assert pushed.tier_links.get().remote_quantity_sold == 4


def test_order_notifications_debounce_to_one_refresh(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    d1 = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    d2 = _deliver(connected, "attendee.updated", f"/events/{pushed.remote_id}/attendees/1/")
    before = sum(1 for c in fake_provider.calls if c[0] == "get_event")
    webhook_service.handle_delivery(d1.id)
    webhook_service.handle_delivery(d2.id)
    after = sum(1 for c in fake_provider.calls if c[0] == "get_event")
    assert after - before == 1  # the pair collapsed into one refresh fetch
    d1.refresh_from_db()
    d2.refresh_from_db()
    assert d1.outcome == d2.outcome == WebhookDelivery.Outcome.PROCESSED


def test_status_matrix(connected: PlatformConnection, pushed: EventLink) -> None:
    assert pushed.remote_status == EventLink.RemoteStatus.DRAFT
    d = _deliver(connected, "event.published", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    pushed.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.LIVE
    d = _deliver(connected, "event.unpublished", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    pushed.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.DRAFT
    pushed.remote_status = EventLink.RemoteStatus.CANCELLED
    pushed.save(update_fields=["remote_status"])
    d = _deliver(connected, "event.published", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    pushed.refresh_from_db()
    d.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.CANCELLED and d.outcome == WebhookDelivery.Outcome.IGNORED
    pushed.remote_status = EventLink.RemoteStatus.DRAFT
    pushed.sync_state = EventLink.SyncState.BROKEN
    pushed.save(update_fields=["remote_status", "sync_state"])
    d = _deliver(connected, "event.published", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    pushed.refresh_from_db()
    d.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.DRAFT and d.outcome == WebhookDelivery.Outcome.IGNORED


def test_retryable_failure_releases_debounce_key_and_leaves_delivery_received(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    fake_provider.fail_once["get_event"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with pytest.raises(RetryableProviderError):
        webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.RECEIVED
    assert cache.get(sync_service.counts_debounce_key(pushed.id)) is None
    webhook_service.handle_delivery(d.id)  # a Celery retry would land here
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.PROCESSED


def test_publish_on_already_live_link_is_ignored(connected: PlatformConnection, pushed: EventLink) -> None:
    pushed.remote_status = EventLink.RemoteStatus.LIVE
    pushed.save(update_fields=["remote_status"])
    d = _deliver(connected, "event.published", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    pushed.refresh_from_db()
    d.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.LIVE and d.outcome == WebhookDelivery.Outcome.IGNORED


def test_unknown_event_and_ignored_kind(connected: PlatformConnection, pushed: EventLink) -> None:
    d = _deliver(connected, "order.placed", "/events/nope/")
    webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.IGNORED
    d = _deliver(connected, "venue.updated", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.IGNORED


def test_resolution_failure_marks_failed_and_reraises(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    fake_provider.fail["resolve_notification"] = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "boom")
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with pytest.raises(ProviderError):
        webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED


def test_inactive_connection_is_ignored(connected: PlatformConnection, pushed: EventLink) -> None:
    connection_service.mark_revoked(connected)
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.IGNORED
