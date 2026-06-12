"""Celery tasks for admin alerts on new-organization creation (Pushover, Discord)."""

import typing as t

import httpx
import structlog
from celery import shared_task
from django.conf import settings

from events.models import Organization

logger = structlog.get_logger(__name__)


@shared_task(bind=True, max_retries=3, name="events.tasks.notify_admin_new_organization_pushover")
def notify_admin_new_organization_pushover(self: t.Any, organization_id: str) -> dict[str, t.Any]:
    """Send a Pushover notification to admin when a new organization is created.

    Includes the org name, owner's email, and the running total of organizations.
    Logs a warning and returns a skipped status if Pushover is not configured.
    """
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        logger.warning(
            "pushover_not_configured",
            message="PUSHOVER_USER_KEY or PUSHOVER_APP_TOKEN not set in settings",
        )
        return {"status": "skipped", "reason": "pushover_not_configured"}

    org = Organization.objects.select_related("owner").get(id=organization_id)
    org_count = Organization.objects.count()
    message = f"New organization: {org.name}\nOwner: {org.owner.email}\nWe now have {org_count} organizations!"
    payload = {
        "token": settings.PUSHOVER_APP_TOKEN,
        "user": settings.PUSHOVER_USER_KEY,
        "message": message,
        "title": "New Organization",
        "priority": 0,
    }

    try:
        response = httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "pushover_notification_sent",
            channel="organization_created",
            organization_id=organization_id,
            response_status=response.status_code,
        )
        return {"status": "sent", "organization_id": organization_id}
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("pushover_error", organization_id=organization_id, error=str(e))
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60
            raise self.retry(exc=e, countdown=countdown)
        logger.exception("pushover_exception", organization_id=organization_id, error=str(e))
        raise


@shared_task(bind=True, max_retries=3, name="events.tasks.notify_admin_new_organization_discord")
def notify_admin_new_organization_discord(self: t.Any, organization_id: str) -> dict[str, t.Any]:
    """Send a Discord notification when a new organization is created.

    Includes the org name, owner's email, and running total. Mentions are
    neutralized via ``allowed_mentions.parse=[]`` so that an org name
    containing ``@everyone`` or a role mention cannot trigger pings.
    """
    webhook_url = settings.DISCORD_ADMIN_WEBHOOK_URL
    if not webhook_url:
        logger.info("discord_webhook_not_configured")
        return {"status": "skipped", "reason": "discord_webhook_not_configured"}

    org = Organization.objects.select_related("owner").get(id=organization_id)
    org_count = Organization.objects.count()
    payload = {
        "content": (
            f"🏛️ New organization created: **{org.name}** (owner: {org.owner.email}). "
            f"We now have {org_count} organizations."
        ),
        "allowed_mentions": {"parse": []},
    }

    try:
        response = httpx.post(webhook_url, json=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "discord_notification_sent",
            channel="organization_created",
            organization_id=organization_id,
            org_count=org_count,
            response_status=response.status_code,
        )
        return {"status": "sent", "organization_id": organization_id, "org_count": org_count}
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("discord_error", channel="organization_created", organization_id=organization_id, error=str(e))
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60
            raise self.retry(exc=e, countdown=countdown)
        logger.exception(
            "discord_exception", channel="organization_created", organization_id=organization_id, error=str(e)
        )
        raise
