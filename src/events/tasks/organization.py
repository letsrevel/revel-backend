"""Celery tasks for organization contact email verification and message forwarding."""

from uuid import UUID

import structlog
from celery import shared_task
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from common.models import SiteSettings
from common.tasks import send_email
from events.models import OrganizationContactMessage

logger = structlog.get_logger(__name__)


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
    verification_link = (
        SiteSettings.get_solo().frontend_base_url + f"/org/{organization_slug}/verify-contact-email?token={token}"
    )
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
    admin_link = (
        SiteSettings.get_solo().frontend_base_url + f"/org/{organization.slug}/admin/contact-messages/{message.id}"
    )
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
