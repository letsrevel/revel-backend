"""Celery entry points for the integrations app. Every task pins ``name=``."""

import typing as t
from uuid import UUID

import structlog
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from integrations import registry
from integrations.exceptions import RetryableProviderError

logger = structlog.get_logger(__name__)

MAX_RETRIES = 5
WEBHOOK_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 600


def _retry_countdown(retries: int) -> int:
    """Exponential backoff, capped, for the next push attempt."""
    return min(int(RETRY_BACKOFF_SECONDS * 2**retries), RETRY_BACKOFF_MAX_SECONDS)


@shared_task(bind=True, name="integrations.push_event_link", max_retries=MAX_RETRIES)
def push_event_link(self: t.Any, link_id: str) -> None:
    """Push one event link's full state to its platform (spec §7.3).

    The link row must never lie: a transient failure keeps it ``pending`` and schedules a retry,
    an exhausted retry budget or any unexpected exception writes ``failed`` before propagating.

    Args:
        self: Celery task instance (automatically passed when bind=True).
        link_id: UUID of the ``EventLink`` to push.
    """
    from integrations.models import EventLink
    from integrations.service import sync_service

    link = EventLink.objects.select_related("event", "connection", "event__organization").filter(id=link_id).first()
    if link is None:
        logger.info("integration_push_skipped_missing_link", link_id=link_id)
        return
    if link.connection.status != link.connection.Status.ACTIVE:
        logger.info("integration_push_skipped_inactive_connection", link_id=link_id)
        return
    if link.connection.provider not in registry.PROVIDERS:
        logger.info("integration_push_skipped_disabled_provider", link_id=link_id, provider=link.connection.provider)
        return
    try:
        sync_service.push_link(link)
    except RetryableProviderError as e:
        sync_service.note_retry(link, e)
        try:
            raise self.retry(exc=e, countdown=_retry_countdown(self.request.retries))
        except MaxRetriesExceededError, RetryableProviderError:
            # Celery raises MaxRetriesExceededError when the budget is spent — except when an
            # ``exc`` is passed, where it re-raises that exception instead (which it also does
            # whenever the task is called directly). Either way the row must not stay pending.
            if self.request.retries >= MAX_RETRIES:
                sync_service.note_retry(link, e, exhausted=True)
            raise
    except Exception as e:
        sync_service.note_failure(link, e)
        raise


@shared_task(name="integrations.import_remote_event")
def import_remote_event(job_id: str) -> None:
    """Run one queued ``ImportJob`` (spec §7.6); the row records the outcome either way."""
    from integrations.models import ImportJob
    from integrations.service import import_service

    job = ImportJob.objects.select_related("connection__organization").filter(id=job_id).first()
    if job is None:
        logger.info("integration_import_skipped_missing_job", job_id=job_id)
        return
    if job.status != ImportJob.Status.QUEUED:
        # A plain read, not a claim: two concurrent deliveries can both pass it, and the
        # (connection, remote_id) uniqueness race inside import_remote_event settles that case.
        logger.info("integration_import_skipped_already_run", job_id=job_id, status=job.status)
        return
    import_service.run_import_job(job)


@shared_task(bind=True, name="integrations.handle_webhook_delivery", max_retries=WEBHOOK_MAX_RETRIES)
def handle_webhook_delivery(self: t.Any, delivery_id: str) -> None:
    """Process one recorded webhook delivery (spec §8).

    ``handle_delivery`` leaves a retryable failure RECEIVED so the retry can finish it, so this
    task owns the other half of that contract: once the budget is spent the row is marked FAILED
    rather than claiming pending work forever.

    Args:
        self: Celery task instance (automatically passed when bind=True).
        delivery_id: UUID (as a string) of the ``WebhookDelivery`` row to process.
    """
    from integrations.models import WebhookDelivery
    from integrations.service import webhook_service

    if not WebhookDelivery.objects.filter(id=delivery_id).exists():
        logger.info("integration_webhook_skipped_missing_delivery", delivery_id=delivery_id)
        return
    try:
        webhook_service.handle_delivery(UUID(delivery_id))
    except RetryableProviderError as e:
        try:
            raise self.retry(exc=e, countdown=_retry_countdown(self.request.retries))
        except MaxRetriesExceededError, RetryableProviderError:
            # Same shape as push_event_link: Celery re-raises the passed ``exc`` instead of
            # MaxRetriesExceededError, and does so too when the task is called directly.
            if self.request.retries >= WEBHOOK_MAX_RETRIES:
                webhook_service.mark_exhausted(UUID(delivery_id))
            raise


@shared_task(
    name="integrations.refresh_link_counts",
    autoretry_for=(RetryableProviderError,),
    retry_backoff=RETRY_BACKOFF_SECONDS,
    retry_backoff_max=RETRY_BACKOFF_MAX_SECONDS,
    max_retries=3,
)
def refresh_link_counts(link_id: str) -> None:
    """Trailing edge of the counts debounce (spec §7.8).

    Scheduled after an immediate refresh so the last order of a burst — every notification
    that arrived inside the debounce window and was answered without a fetch — is still
    reflected in the stored counts.

    Args:
        link_id: UUID (as a string) of the ``EventLink`` whose counts to refresh.
    """
    from integrations.models import EventLink
    from integrations.service import sync_service

    link = EventLink.objects.select_related("connection").filter(id=link_id).first()
    if link is None:
        logger.info("integration_counts_skipped_missing_link", link_id=link_id)
        return
    if link.connection.status != link.connection.Status.ACTIVE:
        logger.info("integration_counts_skipped_inactive_connection", link_id=link_id)
        return
    if link.connection.provider not in registry.PROVIDERS:
        logger.info("integration_counts_skipped_disabled_provider", link_id=link_id, provider=link.connection.provider)
        return
    sync_service.refresh_counts(link)


@shared_task(name="integrations.reconcile_counts")
def reconcile_counts() -> dict[str, int]:
    """Beat: 15-minute count reconcile (spec §7.8), budget-aware (§7.7a)."""
    from integrations.service import reconcile_service

    summary = reconcile_service.reconcile_counts()
    return {"refreshed": summary.refreshed, "skipped_for_budget": summary.skipped_for_budget, "failed": summary.failed}


@shared_task(name="integrations.prune_webhook_deliveries")
def prune_webhook_deliveries() -> int:
    """Beat: daily retention sweep of the audit rows (webhook deliveries and finished import jobs)."""
    from integrations.service import reconcile_service

    return reconcile_service.prune_webhook_deliveries() + reconcile_service.prune_import_jobs()
