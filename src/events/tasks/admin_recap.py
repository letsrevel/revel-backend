"""Daily admin activity recap, delivered to Pushover and the admin Discord webhook."""

import typing as t

import httpx
import structlog
from celery import shared_task
from django.conf import settings

from events.service import admin_recap_service

logger = structlog.get_logger(__name__)


@shared_task(bind=True, max_retries=3, name="events.tasks.send_daily_admin_recap")
def send_daily_admin_recap(self: t.Any) -> dict[str, str]:
    """Send the previous local day's activity recap to each configured admin channel.

    Skips a channel when it is not configured. Mentions are neutralized for Discord because
    organization names are user-controlled.

    Returns:
        Map of channel to ``sent`` or ``skipped``.
    """
    message = admin_recap_service.format_daily_recap(admin_recap_service.build_daily_recap())
    result = {"pushover": "skipped", "discord": "skipped"}

    try:
        if settings.PUSHOVER_USER_KEY and settings.PUSHOVER_APP_TOKEN:
            httpx.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": settings.PUSHOVER_APP_TOKEN,
                    "user": settings.PUSHOVER_USER_KEY,
                    "message": message,
                    "title": "Revel daily recap",
                    "priority": 0,
                },
                timeout=10.0,
            ).raise_for_status()
            result["pushover"] = "sent"
        if settings.DISCORD_ADMIN_WEBHOOK_URL:
            httpx.post(
                settings.DISCORD_ADMIN_WEBHOOK_URL,
                json={"content": message, "allowed_mentions": {"parse": []}},
                timeout=10.0,
            ).raise_for_status()
            result["discord"] = "sent"
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("daily_recap_error", error=str(e))
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=2**self.request.retries * 60)
        raise

    logger.info("daily_recap_sent", **result)
    return result
