"""Celery tasks for blacklist entries."""

import structlog
from celery import Task, shared_task
from django.db import DatabaseError, transaction

from accounts.models import RevelUser
from events.service.blacklist_service import link_blacklist_entries_for_user

logger = structlog.get_logger(__name__)


def link_after_verification(user_id: str) -> None:
    """Link the blacklist entries that match an account, unless the account is gone, erased or being deleted.

    It locks the account row first (``FOR NO KEY UPDATE``, the order the account deletion uses), so an erasure
    that keeps the entries as unlinked entries cannot commit between this check and the link.
    """
    with transaction.atomic():
        user = (
            RevelUser.objects.select_for_update(no_key=True)
            .filter(pk=user_id, is_active=True, erased_at__isnull=True, deletion_requested_at__isnull=True)
            .first()
        )
        if user is None:
            return
        if linked_count := link_blacklist_entries_for_user(user):
            logger.info("blacklist_entries_linked", user_id=user_id, count=linked_count)


@shared_task(
    name="events.link_blacklist_entries_for_user",
    bind=True,
    autoretry_for=(DatabaseError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def link_blacklist_entries_for_user_task(self: Task, user_id: str) -> None:  # type: ignore[type-arg]
    """Link the blacklist entries that match an account after an email verification, and apply the consequences.

    It runs outside the verifying transaction, which holds the account row ``FOR UPDATE``. A database error (a lock
    or statement timeout) is retried. Until the link runs, the ban applies at every blacklist check, because
    enforcement matches the email on every entry; the staff removal waits for the task (normally seconds).
    """
    try:
        link_after_verification(user_id)
    except DatabaseError:
        if self.request.retries >= (self.max_retries or 0):
            logger.error("blacklist_link_after_verification_failed", user_id=user_id)
        raise
