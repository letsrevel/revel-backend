"""Signal handlers for organization membership request notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import OrganizationMembershipRequest
from notifications.enums import NotificationType
from notifications.service.eligibility import get_organization_staff_and_owners
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=OrganizationMembershipRequest)
def handle_membership_request_created(
    sender: type[OrganizationMembershipRequest],
    instance: OrganizationMembershipRequest,
    created: bool,
    **kwargs: t.Any,
) -> None:
    """Notify organization owners/staff when someone requests membership.

    Sends MEMBERSHIP_REQUEST_CREATED notification to organization staff and owners.
    """
    if not created:
        return

    def send_request_notifications() -> None:
        organization = instance.organization
        requester = instance.user

        # Get frontend URL
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        frontend_url = f"{frontend_base_url}/organizations/{organization.id}/membership-requests"

        # Notify all staff and owners of the organization
        staff_and_owners = get_organization_staff_and_owners(organization.id)

        for staff_member in staff_and_owners:
            notification_requested.send(
                sender=handle_membership_request_created,
                user=staff_member,
                notification_type=NotificationType.MEMBERSHIP_REQUEST_CREATED,
                context={
                    "request_id": str(instance.id),
                    "organization_id": str(organization.id),
                    "organization_name": organization.name,
                    "requester_id": str(requester.id),
                    "requester_name": requester.get_full_name() or requester.username,
                    "requester_email": requester.email,
                    "request_message": instance.message or "",
                    "frontend_url": frontend_url,
                },
            )

        logger.info(
            "membership_request_notifications_sent",
            request_id=str(instance.id),
            organization_id=str(organization.id),
            recipient_count=len(staff_and_owners),
        )

    transaction.on_commit(send_request_notifications)
