"""Signal handlers for invitation notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import EventInvitation, EventInvitationRequest
from events.tasks import build_attendee_visibility_flags
from notifications.enums import NotificationType
from notifications.service.eligibility import get_organization_staff_and_owners
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=EventInvitation)
def handle_invitation_save(
    sender: type[EventInvitation], instance: EventInvitation, created: bool, **kwargs: t.Any
) -> None:
    """Send notifications after invitation is created."""
    build_attendee_visibility_flags.delay(str(instance.event_id))
    if not created:
        return

    # Send INVITATION_RECEIVED notification to the invited user
    def send_invitation_notification() -> None:
        event = instance.event

        # Build location string
        event_location = event.address or (event.city.name if event.city else "")

        # Build frontend URL
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        frontend_url = f"{frontend_base_url}/events/{event.id}"

        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.INVITATION_RECEIVED,
            context={
                "invitation_id": str(instance.id),
                "event_id": str(event.id),
                "event_name": event.name,
                "event_description": event.description or "",
                "event_start": event.start.isoformat() if event.start else "",
                "event_end": event.end.isoformat() if event.end else "",
                "event_location": event_location,
                "organization_id": str(event.organization.id),
                "organization_name": event.organization.name,
                "personal_message": instance.custom_message or "",
                "rsvp_required": not event.requires_ticket,
                "tickets_required": event.requires_ticket,
                "frontend_url": frontend_url,
            },
        )

    transaction.on_commit(send_invitation_notification)


@receiver(post_save, sender=EventInvitationRequest)
def handle_invitation_request_created(
    sender: type[EventInvitationRequest], instance: EventInvitationRequest, created: bool, **kwargs: t.Any
) -> None:
    """Notify event organizers when someone requests an invitation.

    Sends INVITATION_REQUEST_CREATED notification to event staff and owners.
    """
    if not created:
        return

    def send_request_notifications() -> None:
        event = instance.event
        requester = instance.user

        # Get frontend URL
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        frontend_url = f"{frontend_base_url}/org/{event.organization.slug}/admin/events/{event.id}/invitations"

        # Notify all staff and owners of the event's organization
        staff_and_owners = get_organization_staff_and_owners(event.organization.id)

        for staff_member in staff_and_owners:
            notification_requested.send(
                sender=handle_invitation_request_created,
                user=staff_member,
                notification_type=NotificationType.INVITATION_REQUEST_CREATED,
                context={
                    "request_id": str(instance.id),
                    "event_id": str(event.id),
                    "event_name": event.name,
                    "requester_email": requester.email,
                    "requester_name": requester.get_full_name() or requester.username,
                    "request_message": instance.message or "",
                    "frontend_url": frontend_url,
                },
            )

        logger.info(
            "invitation_request_notifications_sent",
            request_id=str(instance.id),
            event_id=str(event.id),
            recipient_count=len(staff_and_owners),
        )

    transaction.on_commit(send_request_notifications)
