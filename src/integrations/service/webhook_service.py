"""Inbound webhook intake and dispatch (spec §8).

A delivery is authenticated by the connection's path secret, parsed via the provider, and
recorded as an audit row. After commit a Celery task picks it up, resolves it against the
provider's own host with the connection's token, and routes it to a debounced counts refresh
or the remote-status transition matrix.
"""

from uuid import UUID

from django.core.cache import cache
from django.db import transaction
from django.http import Http404, HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from integrations import registry
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import IntegrationError, ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, WebhookDelivery
from integrations.providers.base import NotificationKind, WebhookNotification
from integrations.service import connection_service, sync_service

_STATUS_ON: dict[str, str] = {
    "event_published": EventLink.RemoteStatus.LIVE,
    "event_unpublished": EventLink.RemoteStatus.DRAFT,
}


def record_delivery(provider_key: str, secret: str, request: HttpRequest) -> WebhookDelivery:
    """Resolve the connection by secret, parse the body via the provider, and persist the audit row."""
    provider = registry.get_provider(provider_key)
    conn = PlatformConnection.objects.filter(provider=provider_key, webhook_secret=secret).first()
    if conn is None:
        raise Http404
    try:
        notification = provider.parse_webhook(request)
    except ProviderError as e:
        raise IntegrationError(e.code, str(_("Malformed webhook delivery.")), e.provider_message) from e
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
        refreshed = sync_service.refresh_counts(link)
    except RetryableProviderError:
        cache.delete(key)
        raise
    if not refreshed:
        return _finish(delivery, WebhookDelivery.Outcome.FAILED)
    refresh_link_counts.apply_async(args=(str(link.id),), countdown=sync_service.COUNTS_DEBOUNCE_SECONDS)
    return _finish(delivery, WebhookDelivery.Outcome.PROCESSED)


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
