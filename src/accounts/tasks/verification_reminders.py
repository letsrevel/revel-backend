"""Tasks for the unverified-account lifecycle: reminders, final warnings, deactivation, deletion."""

import typing as t
from datetime import datetime, timedelta

import structlog
from celery import shared_task
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import EmailVerificationReminderTracking, RevelUser
from common.models import SiteSettings
from common.tasks import send_email

logger = structlog.get_logger(__name__)


def mark_reminder_sent(*, user_id: str, reminder_type: str, success: bool, error_message: str | None = None) -> None:
    """Callback function to update tracking after reminder email send attempt.

    Resolved by ``common.tasks._execute_email_callback`` via the ``accounts.tasks``
    package allowlist entry — must stay importable as ``accounts.tasks.mark_reminder_sent``.

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
        else:
            logger.error(
                "invalid_reminder_type",
                user_id=user_id,
                reminder_type=reminder_type,
            )
            return

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
        "frontend_base_url": site_settings.frontend_base_url,
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
        "frontend_base_url": site_settings.frontend_base_url,
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


def _process_reminder_batch(users: t.Any, reminder_age_cutoff: datetime) -> int:
    """Process a batch of users for reminder sending.

    Args:
        users: QuerySet of users to process.
        reminder_age_cutoff: Only send if last reminder was before this time.

    Returns:
        Count of reminders sent.
    """
    count = 0
    for user in users:
        tracking = getattr(user, "verification_reminder_tracking", None)
        if tracking is None:
            tracking = EmailVerificationReminderTracking.objects.create(user=user)
            user.verification_reminder_tracking = tracking  # Cache for subsequent loops
        # Skip if final warning already sent
        if tracking.final_warning_sent_at:
            continue
        # Send if never sent OR sent before cutoff
        if not tracking.last_reminder_sent_at or tracking.last_reminder_sent_at <= reminder_age_cutoff:
            _send_reminder_email(user, "early")
            count += 1
    return count


@shared_task(name="accounts.tasks.send_early_verification_reminders")
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

    # Base queryset: unverified, active, non-guest users with tracking prefetched
    base_qs = RevelUser.objects.filter(email_verified=False, is_active=True, guest=False).select_related(
        "verification_reminder_tracking"
    )

    # 24-hour reminders (account 24h-3d old, last reminder >24h ago OR never sent)
    day_ago = now - timedelta(hours=24)
    three_days_ago = now - timedelta(days=3)
    users_24h = base_qs.filter(date_joined__lte=day_ago, date_joined__gt=three_days_ago)
    stats["24h"] = _process_reminder_batch(users_24h, day_ago)

    # 3-day reminders (account 3d-7d old, last reminder >48h ago)
    two_days_ago = now - timedelta(hours=48)
    seven_days_ago = now - timedelta(days=7)
    users_3d = base_qs.filter(date_joined__lte=three_days_ago, date_joined__gt=seven_days_ago)
    stats["3d"] = _process_reminder_batch(users_3d, two_days_ago)

    # 7-day reminders (account 7d-30d old, last reminder >7d ago)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    users_7d = base_qs.filter(date_joined__lte=seven_days_ago, date_joined__gt=month_ago)
    stats["7d"] = _process_reminder_batch(users_7d, week_ago)

    logger.info("early_verification_reminders_sent", **stats)
    return stats


@shared_task(name="accounts.tasks.send_final_verification_warnings")
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
    ).select_related("verification_reminder_tracking")

    count = 0
    for user in users_needing_final_warning:
        tracking = getattr(user, "verification_reminder_tracking", None)
        if tracking is None:
            tracking = EmailVerificationReminderTracking.objects.create(user=user)
            user.verification_reminder_tracking = tracking  # Cache the created tracking
        # Only send if final warning hasn't been sent yet
        if not tracking.final_warning_sent_at:
            _send_reminder_email(user, "final_warning")
            count += 1
            logger.info("final_verification_warning_queued", user_id=str(user.id), email=user.email)

    logger.info("final_verification_warnings_sent", count=count)
    return {"count": count}


@shared_task(name="accounts.tasks.deactivate_unverified_accounts")
def deactivate_unverified_accounts() -> dict[str, int]:
    """Deactivate accounts that received final warning and still haven't verified.

    Accounts are deactivated at least 24 hours after receiving the final warning
    if they still haven't verified. They receive one more email with verification
    link and deletion link.

    Returns:
        Dict with count of accounts deactivated.
    """
    now = timezone.now()
    grace_period = timedelta(hours=24)
    grace_cutoff = now - grace_period

    # Get all tracking records with final warning sent >24h ago but no deactivation email
    tracking_records = EmailVerificationReminderTracking.objects.filter(
        final_warning_sent_at__lte=grace_cutoff, deactivation_email_sent_at__isnull=True
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


@shared_task(name="accounts.tasks.delete_old_inactive_accounts")
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
