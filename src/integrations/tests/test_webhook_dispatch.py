"""Deliveries are dispatched on commit; kinds route to counts or the status matrix; unknown → ignored."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import orjson
import pytest
from celery.exceptions import Retry
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Event, TicketTier
from integrations import tasks
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


def test_order_notifications_debounce_to_one_refresh_plus_a_trailing_one(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """One immediate fetch for the burst, and one delayed task so the last order is not lost."""
    d1 = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    d2 = _deliver(connected, "attendee.updated", f"/events/{pushed.remote_id}/attendees/1/")
    before = sum(1 for c in fake_provider.calls if c[0] == "get_event")
    with mock.patch.object(tasks.refresh_link_counts, "apply_async") as apply_async:
        webhook_service.handle_delivery(d1.id)
        webhook_service.handle_delivery(d2.id)
    after = sum(1 for c in fake_provider.calls if c[0] == "get_event")
    assert after - before == 1  # the pair collapsed into one refresh fetch
    apply_async.assert_called_once_with(args=(str(pushed.id),), countdown=sync_service.COUNTS_DEBOUNCE_SECONDS)
    d1.refresh_from_db()
    d2.refresh_from_db()
    assert d1.outcome == d2.outcome == WebhookDelivery.Outcome.PROCESSED


def test_trailing_refresh_task_picks_up_the_orders_of_the_window(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """The scheduled task is what makes the debounce lossless: it re-reads the counts."""
    for c in fake_provider.events[pushed.remote_id].ticket_classes:
        c.quantity_sold = 11
    tasks.refresh_link_counts(str(pushed.id))
    assert pushed.tier_links.get().remote_quantity_sold == 11


def test_counts_refresh_failure_fails_the_delivery(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """A non-transient refresh failure is recorded on the delivery, not papered over as processed."""
    fake_provider.fail["get_event"] = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "boom")
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with mock.patch.object(tasks.refresh_link_counts, "apply_async") as apply_async:
        webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED
    apply_async.assert_not_called()  # nothing to trail: the immediate refresh got nowhere


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


def test_revoked_resolution_marks_the_connection_and_fails_the_delivery(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """A 401 while resolving means the token is dead: flag the connection instead of retrying forever."""
    fake_provider.fail["resolve_notification"] = ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with pytest.raises(ProviderError):
        webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    connected.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED
    assert connected.status == PlatformConnection.Status.ERROR


def test_inactive_connection_is_ignored(connected: PlatformConnection, pushed: EventLink) -> None:
    connection_service.mark_revoked(connected)
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.IGNORED


def test_retryable_order_resolution_leaves_the_delivery_received(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """A 429 while resolving an *order* pointer must retry, not fail the delivery permanently.

    The client signals transience with ``retryable=True`` on a plain ``ProviderError``; if that is
    not upgraded to ``RetryableProviderError`` the task's autoretry never fires and the order's
    counts are lost.
    """
    fake_provider.orders["ord-1"] = pushed.remote_id
    fake_provider.fail["resolve_notification"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    d = _deliver(connected, "order.placed", "/orders/ord-1/")

    with pytest.raises(RetryableProviderError):
        webhook_service.handle_delivery(d.id)

    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.RECEIVED


def test_exhausted_retries_mark_the_delivery_failed(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """The row must not claim pending work once the retry budget is spent."""
    fake_provider.orders["ord-2"] = pushed.remote_id
    fake_provider.fail["resolve_notification"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    d = _deliver(connected, "order.placed", "/orders/ord-2/")

    # `apply(retries=...)` runs the task eagerly with a spent budget; celery re-raises the
    # original exception rather than MaxRetriesExceededError because we hand it an ``exc``.
    with pytest.raises(RetryableProviderError):
        tasks.handle_webhook_delivery.apply(args=(str(d.id),), retries=tasks.WEBHOOK_MAX_RETRIES)

    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED


def test_low_shared_budget_defers_delivery_without_resolving(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    """Below the reserve the delivery waits for a Celery retry instead of spending the shared budget."""
    settings.INTEGRATIONS_RATE_RESERVE = 200
    fake_provider.budget = 150
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    before = [c for c in fake_provider.calls if c[0] == "resolve_notification"]
    with pytest.raises(RetryableProviderError) as exc_info:
        webhook_service.handle_delivery(d.id)
    assert exc_info.value.code == IntegrationErrorCode.PROVIDER_RATE_LIMITED
    assert [c for c in fake_provider.calls if c[0] == "resolve_notification"] == before
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.RECEIVED


def test_unknown_budget_still_resolves_delivery(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    settings.INTEGRATIONS_RATE_RESERVE = 200
    fake_provider.budget = None
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.PROCESSED


def _low_budget(fake_provider: FakeProvider, settings: t.Any) -> None:
    settings.INTEGRATIONS_RATE_RESERVE = 200
    fake_provider.budget = 150


def test_budget_defer_reschedules_without_consuming_retries(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    """A budget defer delays the delivery on its own countdown, even past WEBHOOK_MAX_RETRIES."""
    _low_budget(fake_provider, settings)
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async") as apply_async:
        tasks.handle_webhook_delivery.apply(args=(str(d.id),), retries=tasks.WEBHOOK_MAX_RETRIES + 2)
    apply_async.assert_called_once_with(args=(str(d.id),), countdown=tasks.WEBHOOK_BUDGET_DEFER_COUNTDOWN_SECONDS)
    assert tasks.WEBHOOK_BUDGET_DEFER_COUNTDOWN_SECONDS == 15 * 60
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.RECEIVED


def test_budget_defer_gives_up_once_the_delivery_is_too_old(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    _low_budget(fake_provider, settings)
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    WebhookDelivery.objects.filter(id=d.id).update(
        created_at=timezone.now() - tasks.WEBHOOK_BUDGET_DEFER_MAX_AGE - timedelta(minutes=1)
    )
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async") as apply_async:
        tasks.handle_webhook_delivery.apply(args=(str(d.id),))
    apply_async.assert_not_called()
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED


def test_budget_lookup_failure_fails_the_delivery(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider
) -> None:
    """A broken budget read must not leave the row RECEIVED forever."""
    d = _deliver(connected, "order.placed", f"/events/{pushed.remote_id}/")
    with (
        mock.patch.object(fake_provider, "remaining_budget", side_effect=RuntimeError("cache down")),
        pytest.raises(RuntimeError),
    ):
        webhook_service.handle_delivery(d.id)
    d.refresh_from_db()
    assert d.outcome == WebhookDelivery.Outcome.FAILED


def test_trailing_counts_refresh_respects_the_budget_reserve(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    _low_budget(fake_provider, settings)
    before = [c for c in fake_provider.calls if c[0] == "get_event"]
    with pytest.raises(Retry):  # eager autoretry: the budget error is retried (with backoff), not swallowed
        tasks.refresh_link_counts.apply(args=(str(pushed.id),), throw=True)
    assert [c for c in fake_provider.calls if c[0] == "get_event"] == before
