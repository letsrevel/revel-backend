"""Celery entry points for the integrations app. Every task pins ``name=``."""

import structlog
from celery import shared_task

from integrations.exceptions import RetryableProviderError

logger = structlog.get_logger(__name__)


@shared_task(
    name="integrations.push_event_link",
    autoretry_for=(RetryableProviderError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def push_event_link(link_id: str) -> None:
    """Push one event link's full state to its platform (spec §7.3)."""
    from integrations.models import EventLink
    from integrations.service import sync_service

    link = EventLink.objects.select_related("event", "connection", "event__organization").filter(id=link_id).first()
    if link is None:
        logger.info("integration_push_skipped_missing_link", link_id=link_id)
        return
    if link.connection.status != link.connection.Status.ACTIVE:
        logger.info("integration_push_skipped_inactive_connection", link_id=link_id)
        return
    sync_service.push_link(link)


@shared_task(name="integrations.import_remote_event")
def import_remote_event(connection_id: str, remote_id: str) -> None:
    """Create a Revel draft from a remote event (spec §7.6)."""
    from integrations.models import PlatformConnection
    from integrations.service import sync_service

    conn = (
        PlatformConnection.objects.select_related("organization")
        .filter(id=connection_id, status=PlatformConnection.Status.ACTIVE)
        .first()
    )
    if conn is None:
        logger.info("integration_import_skipped_inactive_connection", connection_id=connection_id)
        return
    sync_service.import_remote_event(conn, remote_id)
