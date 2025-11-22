"""Tasks for the authentication app."""

import traceback
import typing as t
from datetime import timedelta

import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.contrib.sites.models import Site
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from ninja_jwt.token_blacklist.models import OutstandingToken
from ninja_jwt.utils import aware_utcnow

from accounts.models import EmailVerificationReminderTracking, RevelUser, UserDataExport
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


# Email verification reminder tasks


def mark_reminder_sent(*, user_id: str, reminder_type: str, success: bool, error_message: str | None = None) -> None:
    """Callback function to update tracking after reminder email send attempt.

    Args:
        user_id: UUID of the user.
        reminder_type: Either "early", "final_warning", or "deactivation".
        success: Whether the email was sent successfully.
        error_message: Error message if sending failed.
    """
    if not success:
        logger.warning(
            "reminder_email_failed",
            user_id=user_id,
            reminder_type=reminder_type,
            error=error_message,
        )
        return

    try:
        user = RevelUser.objects.get(id=user_id)
        tracking, _ = EmailVerificationReminderTracking.objects.get_or_create(user=user)
        now = timezone.now()

        if reminder_type == "early":
            tracking.last_reminder_sent_at = now
            tracking.save(update_fields=["last_reminder_sent_at"])
        elif reminder_type == "final_warning":
            tracking.final_warning_sent_at = now
            tracking.last_reminder_sent_at = now
            tracking.save(update_fields=["final_warning_sent_at", "last_reminder_sent_at"])
        elif reminder_type == "deactivation":
            tracking.deactivation_email_sent_at = now
            tracking.save(update_fields=["deactivation_email_sent_at"])

        logger.info(
            "reminder_tracking_updated",
            user_id=user_id,
            reminder_type=reminder_type,
        )
    except RevelUser.DoesNotExist:
        logger.warning("reminder_callback_user_not_found", user_id=user_id)


def _send_reminder_email(user: RevelUser, reminder_type: str) -> None:
    """Send verification reminder email with both verification and deletion links.

    Args:
        user: The user to send reminder to.
        reminder_type: Either "early" or "final_warning".
    """
    from accounts.service.account import create_deletion_token, create_verification_token

    verification_token = create_verification_token(user)
    deletion_token = create_deletion_token(user)

    site_settings = SiteSettings.get_solo()
    verification_link = site_settings.frontend_base_url + f"/login/confirm-email?token={verification_token}"
    deletion_link = site_settings.frontend_base_url + f"/account/confirm-deletion?token={deletion_token}"

    if reminder_type == "final_warning":
        subject = render_to_string("accounts/emails/email_verification_final_warning_subject.txt").strip()
        template_base = "accounts/emails/email_verification_final_warning"
    else:
        subject = render_to_string("accounts/emails/email_verification_reminder_subject.txt").strip()
        template_base = "accounts/emails/email_verification_reminder"

    context = {
        "verification_link": verification_link,
        "deletion_link": deletion_link,
        "user": user,
    }

    body = render_to_string(f"{template_base}_body.txt", context)
    html_body = render_to_string(f"{template_base}_body.html", context)

    callback_data = {
        "module": "accounts.tasks",
        "function": "mark_reminder_sent",
        "kwargs": {
            "user_id": str(user.id),
            "reminder_type": reminder_type,
        },
    }

    send_email.delay(to=user.email, subject=subject, body=body, html_body=html_body, callback_data=callback_data)
    logger.info("verification_reminder_queued", user_id=str(user.id), email=user.email, reminder_type=reminder_type)


def _send_deactivation_email(user: RevelUser) -> None:
    """Send account deactivation notice with verification and deletion links.

    Args:
        user: The deactivated user.
    """
    from accounts.service.account import create_deletion_token, create_verification_token

    verification_token = create_verification_token(user)
    deletion_token = create_deletion_token(user)

    site_settings = SiteSettings.get_solo()
    verification_link = site_settings.frontend_base_url + f"/login/confirm-email?token={verification_token}"
    deletion_link = site_settings.frontend_base_url + f"/account/confirm-deletion?token={deletion_token}"

    subject = render_to_string("accounts/emails/account_deactivated_subject.txt").strip()
    context = {
        "verification_link": verification_link,
        "deletion_link": deletion_link,
        "user": user,
    }

    body = render_to_string("accounts/emails/account_deactivated_body.txt", context)
    html_body = render_to_string("accounts/emails/account_deactivated_body.html", context)

    callback_data = {
        "module": "accounts.tasks",
        "function": "mark_reminder_sent",
        "kwargs": {
            "user_id": str(user.id),
            "reminder_type": "deactivation",
        },
    }

    send_email.delay(to=user.email, subject=subject, body=body, html_body=html_body, callback_data=callback_data)
    logger.info("deactivation_email_queued", user_id=str(user.id), email=user.email)


@shared_task
def send_early_verification_reminders() -> dict[str, int]:
    """Send verification reminders for accounts 24h-30d old.

    Backoff schedule:
    - 24h+ old: every 24 hours
    - 3d+ old: every 48 hours
    - 7d+ old: every 7 days

    Returns:
        Dict with counts of reminders sent per stage.
    """
    now = timezone.now()
    stats = {"24h": 0, "3d": 0, "7d": 0}

    # Base queryset: unverified, active, non-guest users
    base_qs = RevelUser.objects.filter(email_verified=False, is_active=True, guest=False)

    # 24-hour reminders (account 24h-3d old, last reminder >24h ago OR never sent)
    day_ago = now - timedelta(hours=24)
    three_days_ago = now - timedelta(days=3)
    users_24h = base_qs.filter(date_joined__lte=day_ago, date_joined__gt=three_days_ago)

    for user in users_24h:
        tracking, _ = EmailVerificationReminderTracking.objects.get_or_create(user=user)
        # Skip if final warning already sent
        if tracking.final_warning_sent_at:
            continue
        # Send if never sent OR sent more than 24h ago
        if not tracking.last_reminder_sent_at or tracking.last_reminder_sent_at <= day_ago:
            _send_reminder_email(user, "early")
            stats["24h"] += 1

    # 3-day reminders (account 3d-7d old, last reminder >48h ago)
    two_days_ago = now - timedelta(hours=48)
    seven_days_ago = now - timedelta(days=7)
    users_3d = base_qs.filter(date_joined__lte=three_days_ago, date_joined__gt=seven_days_ago)

    for user in users_3d:
        tracking, _ = EmailVerificationReminderTracking.objects.get_or_create(user=user)
        # Skip if final warning already sent
        if tracking.final_warning_sent_at:
            continue
        # Send if never sent OR last reminder was sent more than 48h ago
        if not tracking.last_reminder_sent_at or tracking.last_reminder_sent_at <= two_days_ago:
            _send_reminder_email(user, "early")
            stats["3d"] += 1

    # 7-day reminders (account 7d-30d old, last reminder >7d ago)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    users_7d = base_qs.filter(date_joined__lte=seven_days_ago, date_joined__gt=month_ago)

    for user in users_7d:
        tracking, _ = EmailVerificationReminderTracking.objects.get_or_create(user=user)
        # Skip if final warning already sent
        if tracking.final_warning_sent_at:
            continue
        # Send if never sent OR last reminder was sent more than 7d ago
        if not tracking.last_reminder_sent_at or tracking.last_reminder_sent_at <= week_ago:
            _send_reminder_email(user, "early")
            stats["7d"] += 1

    logger.info("early_verification_reminders_sent", **stats)
    return stats


@shared_task
def send_final_verification_warnings() -> dict[str, int]:
    """Send final warning to accounts 30+ days old that never verified.

    This warning is sent ONCE and informs users their account will be
    deactivated shortly if they don't verify.

    Returns:
        Dict with count of final warnings sent.
    """
    now = timezone.now()
    month_ago = now - timedelta(days=30)

    users_needing_final_warning = RevelUser.objects.filter(
        email_verified=False, is_active=True, guest=False, date_joined__lte=month_ago
    )

    count = 0
    for user in users_needing_final_warning:
        tracking, _ = EmailVerificationReminderTracking.objects.get_or_create(user=user)
        # Only send if final warning hasn't been sent yet
        if not tracking.final_warning_sent_at:
            _send_reminder_email(user, "final_warning")
            count += 1
            logger.info("final_verification_warning_queued", user_id=str(user.id), email=user.email)

    logger.info("final_verification_warnings_sent", count=count)
    return {"count": count}


@shared_task
def deactivate_unverified_accounts() -> dict[str, int]:
    """Deactivate accounts that received final warning and still haven't verified.

    Accounts are deactivated shortly after the 30-day mark if they haven't
    verified. They receive one more email with verification link and deletion link.

    Returns:
        Dict with count of accounts deactivated.
    """
    now = timezone.now()

    # Get all tracking records with final warning sent but no deactivation email
    tracking_records = EmailVerificationReminderTracking.objects.filter(
        final_warning_sent_at__isnull=False, deactivation_email_sent_at__isnull=True
    ).select_related("user")

    count = 0
    for tracking in tracking_records:
        user = tracking.user
        # Double-check user is still unverified and active
        if not user.email_verified and user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])

            _send_deactivation_email(user)
            count += 1
            logger.warning(
                "account_deactivated_unverified",
                user_id=str(user.id),
                email=user.email,
                account_age_days=(now - user.date_joined).days,
            )

    logger.info("accounts_deactivated", count=count)
    return {"count": count}


@shared_task
def delete_old_inactive_accounts() -> dict[str, t.Any]:
    """Permanently delete accounts inactive for 60+ days.

    Accounts are deleted if they were deactivated (deactivation_email_sent_at)
    60 or more days ago.

    Returns:
        Dict with count of accounts deleted and list of deleted user info.
    """
    now = timezone.now()
    sixty_days_ago = now - timedelta(days=60)

    # Get all tracking records with deactivation email sent 60+ days ago
    tracking_records = EmailVerificationReminderTracking.objects.filter(
        deactivation_email_sent_at__lte=sixty_days_ago
    ).select_related("user")

    deleted_users = []
    for tracking in tracking_records:
        user = tracking.user
        # Double-check user is still inactive and unverified
        if not user.is_active and not user.email_verified:
            user_info = {
                "user_id": str(user.id),
                "email": user.email,
                "date_joined": user.date_joined.isoformat(),
                "deactivated_at": tracking.deactivation_email_sent_at.isoformat()
                if tracking.deactivation_email_sent_at
                else None,
            }
            deleted_users.append(user_info)
            logger.warning("deleting_old_inactive_account", **user_info)
            user.delete()  # This will cascade delete the tracking record too

    logger.info("old_inactive_accounts_deleted", count=len(deleted_users))
    return {"count": len(deleted_users), "deleted_users": deleted_users}
