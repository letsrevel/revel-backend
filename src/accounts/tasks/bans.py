"""Task for processing domain bans (bulk user deactivation)."""

import typing as t

import structlog
from celery import shared_task

from accounts.models import GlobalBan

logger = structlog.get_logger(__name__)


@shared_task(bind=True, max_retries=3, name="accounts.tasks.process_domain_ban_task")
def process_domain_ban_task(self: t.Any, ban_id: str) -> dict[str, t.Any]:
    """Process a domain ban asynchronously, deactivating all matching users.

    The task is idempotent: already-deactivated users are skipped on retry.

    Args:
        self: Celery task instance (automatically passed when bind=True).
        ban_id: UUID of the GlobalBan instance.

    Returns:
        Dict with domain and deactivated_count.
    """
    from accounts.service.global_ban_service import process_domain_ban

    ban = GlobalBan.objects.get(id=ban_id)

    try:
        count = process_domain_ban(ban)
        logger.info("domain_ban_task_completed", ban_id=ban_id, domain=ban.value, deactivated_count=count)
        return {"domain": ban.value, "deactivated_count": count}
    except Exception as exc:
        logger.error("domain_ban_task_failed", ban_id=ban_id, error=str(exc), retry=self.request.retries)
        raise self.retry(exc=exc, countdown=2**self.request.retries * 60)
