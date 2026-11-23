"""Inbound webhook intake and dispatch (spec §8).

A delivery is authenticated by the connection's path secret, parsed via the provider, and
recorded as an audit row. After commit a Celery task picks it up, resolves it against the
provider's own host with the connection's token, and routes it to a debounced counts refresh
or the remote-status transition matrix.
"""

import time
from uuid import UUID

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from integrations import registry
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import BudgetDeferredError, IntegrationError, ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, WebhookDelivery
from integrations.providers.base import ListingProvider, NotificationKind, WebhookNotification
from integrations.service import connection_service, sync_service

logger = structlog.get_logger(__name__)

_STATUS_ON: dict[str, str] = {
    "event_published": EventLink.RemoteStatus.LIVE,
    "event_unpublished": EventLink.RemoteStatus.DRAFT,
}


_INTAKE_WINDOW_SECONDS = 3600


def _intake_count(conn: PlatformConnection) -> int:
    """Count this delivery in the connection's fixed one-hour window and return the new total."""
    # ponytail: a fixed window lets up to twice the limit through across a window edge; a sliding
    # window is the upgrade if that ever matters.
    key = f"integrations:webhook-intake:{conn.id}:{int(time.time()) // _INTAKE_WINDOW_SECONDS}"
    cache.add(key, 0, _INTAKE_WINDOW_SECONDS + 60)
    return int(cache.incr(key))


def record_delivery(provider_key: str, secret: str, request: HttpRequest) -> WebhookDelivery | None:
    """Resolve the connection by secret, parse the body via the provider, and persist the audit row.

    Above ``INTEGRATIONS_WEBHOOK_INTAKE_LIMIT_PER_HOUR`` deliveries for one connection in the
    current hour, the delivery is logged and answered without a row or a task (returns ``None``).
    The reconcile sweep restores the counts of live links; a status change (publish/unpublish) in a
    dropped delivery is lost until the next status delivery for that event.
    """
    provider = registry.get_provider(provider_key)
    conn = PlatformConnection.objects.filter(provider=provider_key, webhook_secret=secret).first()
    if conn is None:
        raise Http404
    try:
        notification = provider.parse_webhook(request)
    except ProviderError as e:
        raise IntegrationError(e.code, str(_("Malformed webhook delivery.")), e.provider_message) from e
    count = _intake_count(conn)
    if count > settings.INTEGRATIONS_WEBHOOK_INTAKE_LIMIT_PER_HOUR:
        logger.warning(
            "integration_webhook_intake_limit_reached",
            connection_id=str(conn.id),
            organization_id=str(conn.organization_id),
            provider=provider_key,
            action=notification.action,
            count=count,
        )
        return None
    delivery = WebhookDelivery.objects.create(
        connection=conn, action=notification.action, resource_path=notification.resource_path, payload=notification.raw
    )
    from integrations.tasks import handle_webhook_delivery

    delivery_id = str(delivery.id)
    transaction.on_commit(lambda: handle_webhook_delivery.delay(delivery_id))
    return delivery


def apply_status_notification(link: EventLink, kind: NotificationKind) -> bool:
    """The transition matrix: draft⇄live on publish/unpublish; cancelled and broken links never move."""
    target = _STATUS_ON.get(kind)
    if (
        target is None
        or link.remote_status == EventLink.RemoteStatus.CANCELLED
        or link.sync_state == EventLink.SyncState.BROKEN
    ):
        return False
    if link.remote_status == target:
        return False
    link.remote_status = target
    link.save(update_fields=["remote_status", "updated_at"])
    return True


def _finish(delivery: WebhookDelivery, outcome: str) -> WebhookDelivery:
    delivery.outcome = outcome
    delivery.save(update_fields=["outcome", "updated_at"])
    return delivery


def mark_exhausted(delivery_id: UUID) -> None:
    """Mark a delivery FAILED once its retry budget is spent.

    ``handle_delivery`` deliberately leaves a retryable failure RECEIVED so the next attempt can
    finish it; without this the row would claim pending work forever after the last retry.
    """
    WebhookDelivery.objects.filter(id=delivery_id, outcome=WebhookDelivery.Outcome.RECEIVED).update(
        outcome=WebhookDelivery.Outcome.FAILED, updated_at=timezone.now()
    )


def _deferred_slot_key(connection_id: UUID) -> str:
    return f"integrations:webhook-deferred:{connection_id}"


def hold_deferred_slot(connection_id: UUID, delivery_id: str, ttl_seconds: int) -> bool:
    """Claim (or keep) the connection's one deferred-delivery slot for this delivery.

    While the shared budget is low only one delivery per connection waits for it; the caller
    finishes every other deferred delivery with ``finish_coalesced``.
    """
    # ponytail: get-then-set is not atomic; if the TTL ends between the two calls, two deliveries can
    # wait for one cycle, and the next run coalesces one. A Lua compare-and-set is the upgrade.
    key = _deferred_slot_key(connection_id)
    if cache.add(key, delivery_id, ttl_seconds):
        return True
    if cache.get(key) != delivery_id:
        return False
    cache.set(key, delivery_id, ttl_seconds)
    return True


def release_deferred_slot(connection_id: UUID, delivery_id: str) -> None:
    """Free the connection's deferred-delivery slot if this delivery holds it."""
    key = _deferred_slot_key(connection_id)
    if cache.get(key) == delivery_id:
        cache.delete(key)


def finish_coalesced(delivery_id: UUID, connection_id: UUID) -> None:
    """Finish a budget-deferred order delivery while another delivery holds the connection's slot.

    An order delivery only triggers a counts refresh: the held delivery refreshes its link and
    the reconcile sweep restores the counts of every other link once the budget recovers.
    """
    WebhookDelivery.objects.filter(id=delivery_id, outcome=WebhookDelivery.Outcome.RECEIVED).update(
        outcome=WebhookDelivery.Outcome.PROCESSED, updated_at=timezone.now()
    )
    logger.info(
        "integration_webhook_coalesced_budget_low",
        delivery_id=str(delivery_id),
        connection_id=str(connection_id),
        held_by=cache.get(_deferred_slot_key(connection_id)),
    )


def _refresh_counts_debounced(delivery: WebhookDelivery, link: EventLink) -> WebhookDelivery:
    """One fetch per burst, and never a lost order (spec §7.8).

    The first notification of a burst refreshes counts immediately and schedules a trailing
    refresh; every notification landing inside the window is answered without a fetch, because
    that trailing refresh will pick their orders up.
    """
    from integrations.tasks import refresh_link_counts

    key = sync_service.counts_debounce_key(link.id)
    if not cache.add(key, 1, sync_service.COUNTS_DEBOUNCE_TTL_SECONDS):
        return _finish(delivery, WebhookDelivery.Outcome.PROCESSED)
    try:
        # Gate the refresh call itself on either path: an event path never fetched, and an order path's fetch may
        # have pushed the budget under the reserve.
        # The delivery already resolved; skipping is best-effort and the reconcile sweep restores the counts.
        sync_service.raise_if_budget_low(registry.get_provider(link.connection.provider), link.connection.provider)
    except BudgetDeferredError:
        cache.delete(key)
        logger.info("integration_counts_skipped_budget_low", link_id=str(link.id))
        return _finish(delivery, WebhookDelivery.Outcome.PROCESSED)
    try:
        refreshed = sync_service.refresh_counts(link)
    except RetryableProviderError:
        cache.delete(key)
        raise
    if not refreshed:
        return _finish(delivery, WebhookDelivery.Outcome.FAILED)
    refresh_link_counts.apply_async(args=(str(link.id),), countdown=sync_service.COUNTS_DEBOUNCE_SECONDS)
    return _finish(delivery, WebhookDelivery.Outcome.PROCESSED)


def _raise_if_fetch_would_overspend(provider: ListingProvider, key: str, notification: WebhookNotification) -> None:
    """Gate only deliveries that call the provider; a status delivery makes no call and must not be deferred.

    Deferring one would let an older transition land after a newer one.
    """
    if provider.resolves_remotely(notification):
        sync_service.raise_if_budget_low(provider, key)


def handle_delivery(delivery_id: UUID) -> WebhookDelivery:
    """Resolve the pointer with the connection's token, then refresh counts or move the status (spec §8)."""
    delivery = WebhookDelivery.objects.select_related("connection").get(id=delivery_id)
    conn = delivery.connection
    if conn.status != PlatformConnection.Status.ACTIVE or conn.provider not in registry.PROVIDERS:
        return _finish(delivery, WebhookDelivery.Outcome.IGNORED)
    provider = registry.get_provider(conn.provider)
    notification = WebhookNotification(
        action=delivery.action, resource_path=delivery.resource_path, raw=delivery.payload
    )
    try:
        _raise_if_fetch_would_overspend(provider, conn.provider, notification)
        resolved = provider.resolve_notification(conn.token(), notification)
        if resolved.kind == "ignored" or not resolved.remote_event_id:
            return _finish(delivery, WebhookDelivery.Outcome.IGNORED)
        link = (
            EventLink.objects.select_related("connection")
            .filter(connection=conn, remote_id=resolved.remote_event_id)
            .first()
        )
        if link is None:
            return _finish(delivery, WebhookDelivery.Outcome.IGNORED)
        if resolved.kind == "order_changed":
            return _refresh_counts_debounced(delivery, link)
        changed = apply_status_notification(link, resolved.kind)
        return _finish(delivery, WebhookDelivery.Outcome.PROCESSED if changed else WebhookDelivery.Outcome.IGNORED)
    except RetryableProviderError:
        raise  # leave the delivery RECEIVED so the Celery retry can complete it
    except ProviderError as e:
        if e.retryable:
            # Providers signal transience with a flag, not the subclass — `resolve_notification`
            # fetches the order live, so a 429/5xx arrives here as a plain ProviderError. Upgrade
            # it, or the guard above never matches and the task's autoretry_for never fires.
            raise RetryableProviderError(e.code, e.provider_message, retryable=True) from e
        if e.code == IntegrationErrorCode.CONNECTION_REVOKED:
            connection_service.mark_revoked(conn)
        _finish(delivery, WebhookDelivery.Outcome.FAILED)
        raise
    except Exception:
        _finish(delivery, WebhookDelivery.Outcome.FAILED)
        raise
