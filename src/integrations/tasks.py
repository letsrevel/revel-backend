"""Celery entry points for the integrations app. Every task pins ``name=``."""

import typing as t

import structlog
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from integrations import registry
from integrations.exceptions import RetryableProviderError

logger = structlog.get_logger(__name__)

MAX_RETRIES = 5
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
def import_remote_event(connection_id: str, remote_id: str) -> None:
    """Create a Revel draft from a remote event (spec §7.6)."""
    from integrations.models import PlatformConnection
    from integrations.service import import_service

    conn = (
        PlatformConnection.objects.select_related("organization")
        .filter(id=connection_id, status=PlatformConnection.Status.ACTIVE)
        .first()
    )
    if conn is None:
        logger.info("integration_import_skipped_inactive_connection", connection_id=connection_id)
        return
    if conn.provider not in registry.PROVIDERS:
        logger.info("integration_import_skipped_disabled_provider", connection_id=connection_id, provider=conn.provider)
        return
    import_service.import_remote_event(conn, remote_id)
