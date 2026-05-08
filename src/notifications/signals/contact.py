"""Signal handlers for organization contact-form messages."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import OrganizationContactMessage
from events.tasks import send_organization_contact_message_email
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)

_MESSAGE_PREVIEW_CHARS = 200


@receiver(post_save, sender=OrganizationContactMessage)
def handle_contact_message_created(
    sender: type[OrganizationContactMessage],
    instance: OrganizationContactMessage,
    created: bool,
    **kwargs: t.Any,
) -> None:
    """On new contact form submission: dispatch email + in-platform notification.

    The transactional email goes to the org's verified contact mailbox with the
    sender's address as Reply-To. The notification dispatch defaults to IN_APP +
    TELEGRAM so staff don't receive a duplicate email of the message body.
    """
    if not created:
        return

    def fire() -> None:
        organization = instance.organization
        send_organization_contact_message_email.delay(message_id=str(instance.id))

        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        admin_url = f"{frontend_base_url}/org/{organization.slug}/admin/contact-messages/{instance.id}"
        message_preview = instance.message[:_MESSAGE_PREVIEW_CHARS]

        recipients = get_staff_for_notification(organization.id, NotificationType.ORG_CONTACT_MESSAGE_RECEIVED)
        for recipient in recipients:
            notification_requested.send(
                sender=handle_contact_message_created,
                user=recipient,
                notification_type=NotificationType.ORG_CONTACT_MESSAGE_RECEIVED,
                context={
                    "message_id": str(instance.id),
                    "organization_id": str(organization.id),
                    "organization_name": organization.name,
                    "sender_email": instance.sender_email_snapshot,
                    "subject": instance.subject,
                    "message_preview": message_preview,
                    "admin_url": admin_url,
                },
            )

        logger.info(
            "organization_contact_message_dispatched",
            message_id=str(instance.id),
            organization_id=str(organization.id),
            recipient_count=len(recipients),
        )

    transaction.on_commit(fire)
