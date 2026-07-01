"""Celery tasks for organization contact emails, admin notifications and demo-data reset."""

import typing as t
from uuid import UUID

import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from common.models import SiteSettings
from common.tasks import send_email
from events.models import Organization, OrganizationContactMessage

logger = structlog.get_logger(__name__)


class ResetDemoDataResult(t.TypedDict):
    """Status payload returned by ``reset_demo_data``."""

    status: str
    message: str


@shared_task(name="events.reset_demo_data")
def reset_demo_data() -> ResetDemoDataResult:
    """Reset demo data by deleting organizations and example.com users, then re-bootstrapping.

    This task invokes the reset_events management command with --no-input flag.
    Only runs when DEMO_MODE is enabled.

    Returns:
        Dictionary with status information.
    """
    logger.info("Starting demo data reset task...")
    call_command("reset_events", "--no-input")
    logger.info("Demo data reset completed successfully")
    return {"status": "success", "message": "Demo data has been reset"}


@shared_task(name="events.tasks.send_organization_contact_email_verification")
def send_organization_contact_email_verification(
    email: str, token: str, organization_name: str, organization_slug: str
) -> None:
    """Send organization contact email verification.

    Args:
        email: The new contact email to verify
        token: JWT verification token
        organization_name: Name of the organization
        organization_slug: Slug of the organization
    """
    logger.info(
        "organization_contact_email_verification_sending",
        email=email,
        organization_name=organization_name,
    )
    subject = _("Verify contact email for %(organization_name)s") % {"organization_name": organization_name}
    site_settings = SiteSettings.get_solo()
    frontend_base_url = site_settings.frontend_base_url
    verification_link = frontend_base_url + f"/org/{organization_slug}/verify-contact-email?token={token}"
    body = render_to_string(
        "events/emails/organization_contact_email_verification_body.txt",
        {
            "verification_link": verification_link,
            "organization_name": organization_name,
            "contact_email": email,
        },
    )
    html_body = render_to_string(
        "events/emails/organization_contact_email_verification_body.html",
        {
            "verification_link": verification_link,
            "organization_name": organization_name,
            "contact_email": email,
            "frontend_base_url": frontend_base_url,
        },
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("organization_contact_email_verification_sent", email=email)


@shared_task(name="events.tasks.send_organization_contact_message_email")
def send_organization_contact_message_email(message_id: str) -> None:
    """Forward a contact-form submission to an organization's contact mailbox.

    Sends one email with ``Reply-To`` set to the sender so the org can reply
    directly. The org's own ``contact_email`` MUST be verified — the resolver
    invariant guarantees that contact_method=FORM implies a verified email.
    """
    message = OrganizationContactMessage.objects.select_related("organization").get(pk=UUID(message_id))
    organization = message.organization
    if not (organization.contact_email and organization.contact_email_verified):
        logger.warning(
            "organization_contact_message_skipped_unverified",
            message_id=message_id,
            organization_id=str(organization.id),
        )
        return

    subject_text = message.subject.strip() or _("New contact message")
    subject = f"[{organization.name}] {subject_text}"
    site_settings = SiteSettings.get_solo()
    frontend_base_url = site_settings.frontend_base_url
    admin_link = frontend_base_url + f"/org/{organization.slug}/admin/contact-messages/{message.id}"
    body = render_to_string(
        "events/emails/organization_contact_message_body.txt",
        {
            "organization_name": organization.name,
            "sender_email": message.sender_email_snapshot,
            "subject": message.subject,
            "message": message.message,
            "admin_link": admin_link,
        },
    )
    html_body = render_to_string(
        "events/emails/organization_contact_message_body.html",
        {
            "organization_name": organization.name,
            "sender_email": message.sender_email_snapshot,
            "subject": message.subject,
            "message": message.message,
            "admin_link": admin_link,
            "frontend_base_url": frontend_base_url,
        },
    )
    send_email(
        to=organization.contact_email,
        subject=subject,
        body=body,
        html_body=html_body,
        reply_to=[message.sender_email_snapshot],
    )
    logger.info(
        "organization_contact_message_sent",
        message_id=message_id,
        organization_id=str(organization.id),
    )


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
