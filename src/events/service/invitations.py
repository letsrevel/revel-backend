"""Invitation request management for events."""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventInvitationRequest, TicketTier


def create_invitation_request(event: Event, user: RevelUser, message: str | None = None) -> EventInvitationRequest:
    """Create an invitation request.

    Args:
        event: The event to request an invitation for.
        user: The user requesting the invitation.
        message: Optional message from the user explaining why they want to attend.

    Returns:
        The created EventInvitationRequest.

    Raises:
        HttpError: If the event does not accept invitation requests, the user is already invited,
                  a pending request already exists, or the application deadline has passed.
    """
    if not event.accept_invitation_requests:
        raise HttpError(400, str(_("This event does not accept invitation requests.")))

    if timezone.now() > event.effective_apply_deadline:
        raise HttpError(400, str(_("The application deadline has passed.")))

    if EventInvitation.objects.filter(event=event, user=user).exists():
        raise HttpError(400, str(_("You are already invited to this event.")))

    if EventInvitationRequest.objects.filter(
        event=event, user=user, status=EventInvitationRequest.InvitationRequestStatus.PENDING
    ).exists():
        raise HttpError(400, str(_("You have already requested an invitation to this event.")))

    return EventInvitationRequest.objects.create(event=event, user=user, message=message)


@transaction.atomic
def approve_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser, tier: TicketTier | None = None
) -> EventInvitationRequest:
    """Approve an invitation request.

    Args:
        invitation_request: The request to approve.
        decided_by: The user approving the request.
        tier: Optional ticket tier to assign to the invitation.

    Returns:
        The updated EventInvitationRequest.
    """
    invitation_request.status = EventInvitationRequest.InvitationRequestStatus.APPROVED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status", "decided_by"])
    EventInvitation.objects.get_or_create(event=invitation_request.event, user=invitation_request.user, tier=tier)
    return invitation_request


def reject_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser
) -> EventInvitationRequest:
    """Reject an invitation request.

    Args:
        invitation_request: The request to reject.
        decided_by: The user rejecting the request.

    Returns:
        The updated EventInvitationRequest.
    """
    invitation_request.status = EventInvitationRequest.InvitationRequestStatus.REJECTED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status", "decided_by"])
    return invitation_request
