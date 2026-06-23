"""Transactional account email-send tasks (verification, activation, password reset, email change, deletion)."""

import structlog
from celery import shared_task
from django.contrib.sites.models import Site
from django.template.loader import render_to_string

from common.models import SiteSettings
from common.tasks import send_email

logger = structlog.get_logger(__name__)


@shared_task(name="accounts.tasks.send_verification_email")
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


@shared_task(name="accounts.tasks.send_account_activation_link")
def send_account_activation_link(email: str, token: str) -> None:
    """Send an account activation email to a guest user registering for a full account."""
    logger.info("account_activation_email_sending", email=email)
    subject = str(render_to_string("accounts/emails/account_activation_subject.txt"))
    activation_link = SiteSettings.get_solo().frontend_base_url + f"/login/reset-password?token={token}"
    body = render_to_string("accounts/emails/account_activation_body.txt", {"activation_link": activation_link})
    html_body = render_to_string("accounts/emails/account_activation_body.html", {"activation_link": activation_link})
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("account_activation_email_sent", email=email)


@shared_task(name="accounts.tasks.send_password_reset_link")
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


@shared_task(name="accounts.tasks.send_email_change_confirmation")
def send_email_change_confirmation(new_email: str, token: str) -> None:
    """Send the confirmation link to the **new** email address.

    Clicking the link proves the user controls the new mailbox.

    Args:
        new_email: The new email address requested by the user.
        token: The single-use email-change JWT to embed in the confirmation link.
    """
    logger.info("email_change_confirmation_sending", new_email=new_email)
    subject = str(render_to_string("accounts/emails/email_change_confirmation_subject.txt"))
    site_settings = SiteSettings.get_solo()
    confirmation_link = site_settings.frontend_base_url + f"/account/confirm-email-change?token={token}"
    context = {"confirmation_link": confirmation_link, "frontend_base_url": site_settings.frontend_base_url}
    body = render_to_string("accounts/emails/email_change_confirmation_body.txt", context)
    html_body = render_to_string("accounts/emails/email_change_confirmation_body.html", context)
    send_email(to=new_email, subject=subject, body=body, html_body=html_body)
    logger.info("email_change_confirmation_sent", new_email=new_email)


@shared_task(name="accounts.tasks.send_email_change_notice")
def send_email_change_notice(current_email: str, masked_new_email: str) -> None:
    """Notify the **current** email address that a change was requested.

    Informational only — there is no cancel link in v1.

    Args:
        current_email: The user's current email address.
        masked_new_email: The new address in masked form (e.g. ``a***@example.com``).
    """
    logger.info("email_change_notice_sending", current_email=current_email)
    subject = str(render_to_string("accounts/emails/email_change_notice_subject.txt"))
    site_settings = SiteSettings.get_solo()
    context = {"masked_new_email": masked_new_email, "frontend_base_url": site_settings.frontend_base_url}
    body = render_to_string("accounts/emails/email_change_notice_body.txt", context)
    html_body = render_to_string("accounts/emails/email_change_notice_body.html", context)
    send_email(to=current_email, subject=subject, body=body, html_body=html_body)
    logger.info("email_change_notice_sent", current_email=current_email)


@shared_task(name="accounts.tasks.send_email_change_completed_old")
def send_email_change_completed_old(old_email: str, new_email: str) -> None:
    """Notify the **old** address that the email change has completed.

    Args:
        old_email: The address being decommissioned.
        new_email: The address that is now primary on the account.
    """
    logger.info("email_change_completed_old_sending", old_email=old_email)
    subject = str(render_to_string("accounts/emails/email_change_completed_old_subject.txt"))
    site_settings = SiteSettings.get_solo()
    context = {"old_email": old_email, "new_email": new_email, "frontend_base_url": site_settings.frontend_base_url}
    body = render_to_string("accounts/emails/email_change_completed_old_body.txt", context)
    html_body = render_to_string("accounts/emails/email_change_completed_old_body.html", context)
    send_email(to=old_email, subject=subject, body=body, html_body=html_body)
    logger.info("email_change_completed_old_sent", old_email=old_email)


@shared_task(name="accounts.tasks.send_email_change_completed_new")
def send_email_change_completed_new(new_email: str, old_email: str) -> None:
    """Welcome message to the **new** address confirming the change is live.

    Args:
        new_email: The address that is now primary on the account.
        old_email: The previous address (referenced in the body for clarity).
    """
    logger.info("email_change_completed_new_sending", new_email=new_email)
    subject = str(render_to_string("accounts/emails/email_change_completed_new_subject.txt"))
    site_settings = SiteSettings.get_solo()
    context = {"old_email": old_email, "new_email": new_email, "frontend_base_url": site_settings.frontend_base_url}
    body = render_to_string("accounts/emails/email_change_completed_new_body.txt", context)
    html_body = render_to_string("accounts/emails/email_change_completed_new_body.html", context)
    send_email(to=new_email, subject=subject, body=body, html_body=html_body)
    logger.info("email_change_completed_new_sent", new_email=new_email)


@shared_task(name="accounts.tasks.send_account_deletion_link")
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
