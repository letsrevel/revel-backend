"""Daily admin activity recap, delivered to Pushover and the admin Discord webhook.

The beat task builds the message once and fans out one task per configured channel, so a failing channel
neither blocks nor causes a resend to the other.
"""

import typing as t

import httpx
import structlog
from celery import shared_task
from django.conf import settings

from events.service import admin_recap_service

logger = structlog.get_logger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _post(task: t.Any, channel: str, url: str, **kwargs: t.Any) -> dict[str, str]:
    """POST once; retry on 5xx/network errors, give up (log) on 4xx, which is permanent (bad token, too long)."""
    error: Exception
    try:
        httpx.post(url, timeout=10.0, **kwargs).raise_for_status()
        return {"status": "sent", "channel": channel}
    except httpx.HTTPStatusError as e:
        if e.response.status_code < 500:
            logger.error("daily_recap_rejected", channel=channel, status=e.response.status_code)
            return {"status": "failed", "channel": channel}
        error = e
    except httpx.RequestError as e:  # includes timeouts
        error = e
    logger.error("daily_recap_error", channel=channel, error=str(error))
    if task.request.retries < task.max_retries:
        raise task.retry(exc=error, countdown=2**task.request.retries * 60)
    raise error


@shared_task(bind=True, max_retries=3, name="events.tasks.send_admin_recap_pushover")
def send_admin_recap_pushover(self: t.Any, message: str) -> dict[str, str]:
    """Send the recap message to Pushover."""
    return _post(
        self,
        "pushover",
        PUSHOVER_URL,
        data={
            "token": settings.PUSHOVER_APP_TOKEN,
            "user": settings.PUSHOVER_USER_KEY,
            "message": message,
            "title": "Revel daily recap",
            "priority": 0,
        },
    )


@shared_task(bind=True, max_retries=3, name="events.tasks.send_admin_recap_discord")
def send_admin_recap_discord(self: t.Any, message: str) -> dict[str, str]:
    """Send the recap message to the admin Discord webhook; mentions are neutralized (org names are user input)."""
    return _post(
        self,
        "discord",
        settings.DISCORD_ADMIN_WEBHOOK_URL,
        json={"content": message, "allowed_mentions": {"parse": []}},
    )


@shared_task(name="events.tasks.send_daily_admin_recap")
def send_daily_admin_recap() -> dict[str, str]:
    """Build the previous local day's recap and queue it for each configured admin channel.

    Returns:
        Map of channel to ``queued`` or ``skipped``.
    """
    message = admin_recap_service.format_daily_recap(admin_recap_service.build_daily_recap())
    result = {"pushover": "skipped", "discord": "skipped"}
    if settings.PUSHOVER_USER_KEY and settings.PUSHOVER_APP_TOKEN:
        send_admin_recap_pushover.delay(message)
        result["pushover"] = "queued"
    if settings.DISCORD_ADMIN_WEBHOOK_URL:
        send_admin_recap_discord.delay(message)
        result["discord"] = "queued"
    logger.info("daily_recap_dispatched", **result)
    return result
