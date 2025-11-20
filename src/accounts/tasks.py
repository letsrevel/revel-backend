"""Tasks for the authentication app."""

import traceback
import typing as t

import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.contrib.sites.models import Site
from django.db.models import Q
from django.template.loader import render_to_string
from ninja_jwt.token_blacklist.models import OutstandingToken
from ninja_jwt.utils import aware_utcnow

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from common.models import SiteSettings
from common.tasks import send_email
from events.models import EventToken

logger = structlog.get_logger(__name__)


@shared_task
def send_verification_email(email: str, token: str) -> None:
    """Send a verification email."""
    logger.info("verification_email_sending", email=email)
    subject = str(render_to_string("accounts/emails/email_verification_subject.txt"))
    verification_link = SiteSettings.get_solo().frontend_base_url + f"/login/confirm-email?token={token}"
    body = render_to_string("accounts/emails/email_verification_body.txt", {"verification_link": verification_link})
    html_body = render_to_string(
        "accounts/emails/email_verification_body.html", {"verification_link": verification_link}
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("verification_email_sent", email=email)


@shared_task
def send_password_reset_link(email: str, token: str) -> None:
    """Send a password reset email."""
    logger.info("password_reset_email_sending", email=email)
    subject = str(render_to_string("accounts/emails/password_reset_subject.txt"))
    password_reset_link = SiteSettings.get_solo().frontend_base_url + f"/login/reset-password?token={token}"
    body = render_to_string("accounts/emails/password_reset_body.txt", {"password_reset_link": password_reset_link})
    html_body = render_to_string(
        "accounts/emails/password_reset_body.html", {"password_reset_link": password_reset_link}
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("password_reset_email_sent", email=email)


@shared_task
def send_account_deletion_link(email: str, token: str) -> None:
    """Send an account deletion confirmation email."""
    logger.info("account_deletion_email_sending", email=email)
    site = Site.objects.get_current()
    subject = str(render_to_string("accounts/emails/account_delete_subject.txt", {"site_name": site.name}))
    site_settings = SiteSettings.get_solo()
    account_deletion_link = site_settings.frontend_base_url + f"/account/confirm-deletion?token={token}"
    body = render_to_string(
        "accounts/emails/account_delete_body.txt",
        {"account_deletion_link": account_deletion_link, "frontend_base_url": site_settings.frontend_base_url},
    )
    html_body = render_to_string(
        "accounts/emails/account_delete_body.html",
        {"account_deletion_link": account_deletion_link, "frontend_base_url": site_settings.frontend_base_url},
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("account_deletion_email_sent", email=email)


@shared_task
def flush_expired_tokens() -> None:
    """Flushes any expired tokens in the outstanding token list.

    This task is designed to be run periodically to clean up expired tokens.
    """
    logger.info("token_cleanup_started")
    # Get the current time in UTC
    current_time = aware_utcnow()

    # Delete expired tokens
    jwt_deleted, _ = OutstandingToken.objects.filter(expires_at__lte=current_time).delete()
    event_deleted, _ = EventToken.objects.filter(expires_at__lte=current_time).delete()
    logger.info("token_cleanup_completed", jwt_tokens_deleted=jwt_deleted, event_tokens_deleted=event_deleted)


@shared_task
def generate_user_data_export(user_id: str) -> None:
    """Generate a data export for a user."""
    logger.info("gdpr_export_task_started", user_id=user_id)
    user = RevelUser.objects.get(id=user_id)
    try:
        data_export = gdpr.generate_user_data_export(user)
    except Exception as e:
        logger.error("gdpr_export_task_failed", user_id=user_id, error=str(e), exc_info=True)
        _notify_data_export_failed(user, traceback.format_exc())
        return
    logger.info("gdpr_export_task_completed", user_id=user_id, export_id=str(data_export.id))
    _notify_user_data_export_ready(data_export)


def _notify_data_export_failed(user: RevelUser, error: str) -> None:
    logger.info("gdpr_export_notification_failed", user_id=str(user.id), email=user.email)
    data_export, _ = UserDataExport.objects.get_or_create(user=user)
    data_export.status = UserDataExport.Status.FAILED
    data_export.error_message = error
    data_export.save(update_fields=["status", "error_message"])

    # Notify the user that something went wrong
    subject = str(render_to_string("accounts/emails/data_export_failed_subject.txt"))
    body = render_to_string("accounts/emails/data_export_failed_body.txt", {"user": user})
    html_body = render_to_string("accounts/emails/data_export_failed_body.html", {"user": user})
    send_email(to=user.email, subject=subject, body=body, html_body=html_body)

    # Notify admins
    subject = str(render_to_string("accounts/emails/data_export_failed_admin_subject.txt"))
    admins = RevelUser.objects.filter(Q(is_superuser=True) | Q(is_staff=True))
    admin_count = admins.count()
    logger.info("gdpr_export_admin_notification_sending", user_id=str(user.id), admin_count=admin_count)
    for admin in admins:
        body = render_to_string(
            "accounts/emails/data_export_failed_admin_body.txt",
            {"user": user, "error_message": data_export.error_message},
        )
        html_body = render_to_string(
            "accounts/emails/data_export_failed_admin_body.html",
            {"user": user, "error_message": data_export.error_message},
        )
        send_email(to=admin.email, subject=subject, body=body, html_body=html_body)


def _notify_user_data_export_ready(data_export: UserDataExport) -> None:
    logger.info(
        "gdpr_export_notification_ready",
        user_id=str(data_export.user.id),
        email=data_export.user.email,
        export_id=str(data_export.id),
    )
    download_url = settings.BASE_URL + data_export.file.url
    subject = "Your Revel Data Export is Ready"
    body = render_to_string(
        "accounts/emails/data_export_ready_body.txt", {"download_url": download_url, "user": data_export.user}
    )
    html_body = render_to_string(
        "accounts/emails/data_export_ready_body.html", {"download_url": download_url, "user": data_export.user}
    )
    send_email(to=data_export.user.email, subject=subject, body=body, html_body=html_body)


@shared_task
def delete_user_account(user_id: str) -> None:
    """Delete a user account and all associated data in the background.

    This task is designed to handle heavy deletion operations that may involve
    many database relationships. The deletion is performed in a transaction
    to ensure data consistency.

    Args:
        user_id: The UUID of the user to delete.
    """
    user = RevelUser.objects.get(id=user_id)
    logger.info("account_deletion_started", user_id=str(user.id), email=user.email)
    try:
        user.delete()
        logger.info("account_deletion_completed", user_id=user_id)
    except Exception as e:
        logger.error("account_deletion_failed", user_id=user_id, error=str(e), exc_info=True)
        raise


@shared_task(bind=True, max_retries=3)
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
    # Check if Pushover is configured
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        logger.warning(
            "pushover_not_configured",
            message="PUSHOVER_USER_KEY or PUSHOVER_APP_TOKEN not set in settings",
        )
        return {"status": "skipped", "reason": "pushover_not_configured"}

    # Build notification message
    user_type_label = "guest user" if is_guest else "user"
    message = f"New {user_type_label} joined: {user_email}"
    title = "New Guest User" if is_guest else "New User"

    # Prepare Pushover API request
    pushover_url = "https://api.pushover.net/1/messages.json"
    payload = {
        "token": settings.PUSHOVER_APP_TOKEN,
        "user": settings.PUSHOVER_USER_KEY,
        "message": message,
        "title": title,
        "priority": 0,  # Normal priority
    }

    try:
        # Send notification via Pushover API
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
