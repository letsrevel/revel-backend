"""Admin notification tasks (Pushover + Discord) for new user signups."""

import typing as t

import httpx
import structlog
from celery import shared_task
from django.conf import settings

from accounts.models import RevelUser

logger = structlog.get_logger(__name__)


def _get_referrer_email(user_id: str) -> str | None:
    """Return the email of the user who referred `user_id`, or None if none."""
    from uuid import UUID

    from accounts.models import Referral

    referral = Referral.objects.select_related("referrer").filter(referred_user_id=UUID(user_id)).first()
    return referral.referrer.email if referral else None


@shared_task(bind=True, max_retries=3, name="accounts.tasks.notify_admin_new_user_joined")
def notify_admin_new_user_joined(self: t.Any, user_id: str, user_email: str, is_guest: bool) -> dict[str, t.Any]:
    """Send Pushover notification to admin when a new user joins the platform.

    This is a standalone notification system separate from the main notification infrastructure.
    No database records are created - this simply sends a Pushover notification if configured.

    Args:
        self: Celery task instance (automatically passed when bind=True)
        user_id: UUID of the user who joined
        user_email: Email of the user who joined
        is_guest: Whether the user is a guest user

    Returns:
        Dict with notification result

    Raises:
        Exception: If Pushover API call fails after retries
    """
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        logger.warning(
            "pushover_not_configured",
            message="PUSHOVER_USER_KEY or PUSHOVER_APP_TOKEN not set in settings",
        )
        return {"status": "skipped", "reason": "pushover_not_configured"}

    user_type_label = "guest user" if is_guest else "user"
    title = "New Guest User" if is_guest else "New User"
    referrer_email = _get_referrer_email(user_id)
    user_count = RevelUser.objects.filter(guest=False).count()

    lines = [f"New {user_type_label} joined: {user_email}"]
    if referrer_email:
        lines.append(f"Referred by: {referrer_email}")
    lines.append(f"We now have {user_count} users!")
    message = "\n".join(lines)

    pushover_url = "https://api.pushover.net/1/messages.json"
    payload = {
        "token": settings.PUSHOVER_APP_TOKEN,
        "user": settings.PUSHOVER_USER_KEY,
        "message": message,
        "title": title,
        "priority": 0,
    }

    try:
        response = httpx.post(pushover_url, data=payload, timeout=10.0)
        response.raise_for_status()

        logger.info(
            "pushover_notification_sent",
            user_id=user_id,
            user_email=user_email,
            is_guest=is_guest,
            response_status=response.status_code,
        )

        return {
            "status": "sent",
            "user_id": user_id,
            "user_email": user_email,
            "is_guest": is_guest,
        }

    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error(
            "pushover_error",
            user_id=user_id,
            error=str(e),
        )
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60  # 1min, 2min, 4min
            logger.info(
                "retrying_pushover_notification",
                user_id=user_id,
                countdown=countdown,
                retry_count=self.request.retries,
            )
            raise self.retry(exc=e, countdown=countdown)
        else:
            logger.exception(
                "pushover_exception",
                user_id=user_id,
                error=str(e),
            )
            raise


@shared_task(bind=True, max_retries=3, name="accounts.tasks.notify_admin_new_user_joined_discord")
def notify_admin_new_user_joined_discord(self: t.Any) -> dict[str, t.Any]:
    """Send a PII-free Discord notification to admin when a new user joins.

    The message never contains the user's email, name, or id — only the running
    total count of non-guest users.

    Returns:
        Dict with notification result

    Raises:
        Exception: If the Discord webhook call fails after retries.
    """
    webhook_url = settings.DISCORD_ADMIN_WEBHOOK_URL
    if not webhook_url:
        logger.info("discord_webhook_not_configured")
        return {"status": "skipped", "reason": "discord_webhook_not_configured"}

    user_count = RevelUser.objects.filter(guest=False).count()
    payload = {
        "content": f"🎉 A new user joined! We now have {user_count} users.",
        "allowed_mentions": {"parse": []},
    }

    try:
        response = httpx.post(webhook_url, json=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "discord_notification_sent",
            channel="user_joined",
            user_count=user_count,
            response_status=response.status_code,
        )
        return {"status": "sent", "user_count": user_count}
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("discord_error", channel="user_joined", error=str(e))
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60
            raise self.retry(exc=e, countdown=countdown)
        logger.exception("discord_exception", channel="user_joined", error=str(e))
        raise
