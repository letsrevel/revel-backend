"""Tasks for the authentication app."""

import traceback

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


@shared_task
def send_verification_email(email: str, token: str) -> None:
    """Send a verification email."""
    subject = str(render_to_string("accounts/emails/email_verification_subject.txt"))
    verification_link = SiteSettings.get_solo().frontend_base_url + f"/login/confirm-email?token={token}"
    body = render_to_string("accounts/emails/email_verification_body.txt", {"verification_link": verification_link})
    html_body = render_to_string(
        "accounts/emails/email_verification_body.html", {"verification_link": verification_link}
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)


@shared_task
def send_password_reset_link(email: str, token: str) -> None:
    """Send a password reset email."""
    subject = str(render_to_string("accounts/emails/password_reset_subject.txt"))
    password_reset_link = SiteSettings.get_solo().frontend_base_url + f"/login/reset-password?token={token}"
    body = render_to_string("accounts/emails/password_reset_body.txt", {"password_reset_link": password_reset_link})
    html_body = render_to_string(
        "accounts/emails/password_reset_body.html", {"password_reset_link": password_reset_link}
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)


@shared_task
def send_account_deletion_link(email: str, token: str) -> None:
    """Send an account deletion confirmation email."""
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


@shared_task
def flush_expired_tokens() -> None:
    """Flushes any expired tokens in the outstanding token list.

    This task is designed to be run periodically to clean up expired tokens.
    """
    # Get the current time in UTC
    current_time = aware_utcnow()

    # Delete expired tokens
    OutstandingToken.objects.filter(expires_at__lte=current_time).delete()
    EventToken.objects.filter(expires_at__lte=current_time).delete()


@shared_task
def generate_user_data_export(user_id: str) -> None:
    """Generate a data export for a user."""
    user = RevelUser.objects.get(id=user_id)
    try:
        data_export = gdpr.generate_user_data_export(user)
    except Exception:
        _notify_data_export_failed(user, traceback.format_exc())
        return
    _notify_user_data_export_ready(data_export)


def _notify_data_export_failed(user: RevelUser, error: str) -> None:
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
    user.delete()
