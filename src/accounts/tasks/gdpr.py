"""Tasks for GDPR user-data exports and account deletion."""

import traceback
from datetime import timedelta

import structlog
from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from common.signing import generate_signed_url
from common.tasks import send_email

# 7 days in seconds for data export download links
DATA_EXPORT_URL_EXPIRES_IN = 7 * 24 * 60 * 60

logger = structlog.get_logger(__name__)


@shared_task(name="accounts.tasks.generate_user_data_export")
def generate_user_data_export(user_id: str) -> None:
    """Generate a data export for a user."""
    logger.info("gdpr_export_task_started", user_id=user_id)
    user = RevelUser.objects.get(id=user_id)
    try:
        data_export = gdpr.generate_user_data_export(user)
    except Exception as e:
        logger.error("gdpr_export_task_failed", user_id=user_id, error=str(e), exc_info=True)
        _notify_data_export_failed(user, traceback.format_exc())
        raise  # Re-raise so Celery marks task as failed for monitoring/alerting
    logger.info("gdpr_export_task_completed", user_id=user_id, export_id=str(data_export.id))
    _notify_user_data_export_ready(data_export)


def _notify_data_export_failed(user: RevelUser, error: str) -> None:
    logger.info("gdpr_export_notification_failed", user_id=str(user.id), email=user.email)
    data_export, _ = UserDataExport.objects.get_or_create(user=user)
    data_export.status = UserDataExport.UserDataExportStatus.FAILED
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
    signed_path = generate_signed_url(data_export.file.name, expires_in=DATA_EXPORT_URL_EXPIRES_IN)
    download_url = settings.BASE_URL + signed_path
    subject = "Your Revel Data Export is Ready"
    body = render_to_string(
        "accounts/emails/data_export_ready_body.txt", {"download_url": download_url, "user": data_export.user}
    )
    html_body = render_to_string(
        "accounts/emails/data_export_ready_body.html", {"download_url": download_url, "user": data_export.user}
    )
    send_email(to=data_export.user.email, subject=subject, body=body, html_body=html_body)


@shared_task(name="accounts.tasks.delete_user_account")
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


@shared_task(name="accounts.tasks.cleanup_expired_data_exports")
def cleanup_expired_data_exports() -> dict[str, int]:
    """Delete files from expired data exports while preserving database records.

    Data export download links are valid for 7 days. After that, the file is no longer
    accessible via signed URL, so we can safely delete it to reclaim storage.

    The database record is preserved for auditing purposes (shows user requested an export).

    Returns:
        Dict with count of files deleted.
    """
    now = timezone.now()
    expiry_cutoff = now - timedelta(seconds=DATA_EXPORT_URL_EXPIRES_IN)

    # Find exports with files that are older than the URL expiry time
    expired_exports = UserDataExport.objects.filter(
        completed_at__lte=expiry_cutoff,
        status=UserDataExport.UserDataExportStatus.READY,
    ).exclude(file="")

    count = 0
    for export in expired_exports:
        export.file.delete(save=False)
        export.file = None
        export.save(update_fields=["file", "updated_at"])
        count += 1
        logger.info(
            "data_export_file_deleted",
            export_id=str(export.id),
            user_id=str(export.user_id),
            completed_at=export.completed_at.isoformat() if export.completed_at else None,
        )

    logger.info("data_export_cleanup_completed", files_deleted=count)
    return {"files_deleted": count}
