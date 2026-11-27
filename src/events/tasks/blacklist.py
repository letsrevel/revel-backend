"""Celery tasks for blacklist entries."""

import structlog
from celery import shared_task

from accounts.models import RevelUser
from events.service.blacklist_service import link_blacklist_entries_for_user

logger = structlog.get_logger(__name__)


@shared_task(
    name="events.link_blacklist_entries_for_user",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def link_blacklist_entries_for_user_task(user_id: str) -> None:
    """Link the blacklist entries that match an account and apply the consequences (after an email verification).

    It runs outside the verifying transaction, which holds the account row ``FOR UPDATE``. A failure is retried,
    so the consequences (staff removal, a BANNED membership) are not lost.
    """
    user = RevelUser.objects.filter(pk=user_id).first()
    if user is None:
        return
    if linked_count := link_blacklist_entries_for_user(user):
        logger.info("blacklist_entries_linked", user_id=user_id, count=linked_count)
