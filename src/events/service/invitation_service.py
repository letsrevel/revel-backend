import typing as t
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404

from accounts.models import RevelUser
from events.models import Event, EventInvitation, PendingEventInvitation, TicketTier
from events.schema import DirectInvitationCreateSchema


@transaction.atomic
def create_direct_invitations(
    event: Event,
    invitation_data: DirectInvitationCreateSchema,
) -> dict[str, int]:
    """Create direct invitations for a list of email addresses.

    For existing users, creates EventInvitation objects.
    For non-existing users, creates PendingEventInvitation objects.

    Returns a summary of created invitations.
    """
    # Validate tier if provided
    tier = None
    if invitation_data.tier_id:
        tier = get_object_or_404(TicketTier, pk=invitation_data.tier_id, event=event)

    invitation_fields = _get_invitation_fields(invitation_data, tier)
    created_invitations = 0
    pending_invitations = 0

    for email_str in invitation_data.emails:
        email = str(email_str).strip().lower()

        if user := RevelUser.objects.filter(email=email).first():
            # User exists - create EventInvitation
            if _create_or_update_event_invitation(event, user, invitation_fields):
                created_invitations += 1
        elif _create_or_update_pending_invitation(event, email, invitation_fields):
            pending_invitations += 1

    # TODO: Send notifications if invitation_data.send_notification is True
    # This will be implemented in a separate task

    return {
        "created_invitations": created_invitations,
        "pending_invitations": pending_invitations,
        "total_invited": created_invitations + pending_invitations,
    }


def _get_invitation_fields(invitation_data: DirectInvitationCreateSchema, tier: TicketTier | None) -> dict[str, t.Any]:
    """Extract invitation fields from schema."""
    return {
        "waives_questionnaire": invitation_data.waives_questionnaire,
        "waives_purchase": invitation_data.waives_purchase,
        "overrides_max_attendees": invitation_data.overrides_max_attendees,
        "waives_membership_required": invitation_data.waives_membership_required,
        "waives_rsvp_deadline": invitation_data.waives_rsvp_deadline,
        "custom_message": invitation_data.custom_message,
        "tier": tier,
    }


def _create_or_update_event_invitation(event: Event, user: RevelUser, invitation_fields: dict[str, t.Any]) -> bool:
    """Create or update an EventInvitation for an existing user. Returns True if created/updated."""
    EventInvitation.objects.update_or_create(
        event=event,
        user=user,
        defaults=invitation_fields,
    )
    return True


def _create_or_update_pending_invitation(event: Event, email: str, invitation_fields: dict[str, t.Any]) -> bool:
    """Create or update a PendingEventInvitation for a non-existing user. Returns True if created/updated."""
    PendingEventInvitation.objects.update_or_create(
        event=event,
        email=email,
        defaults=invitation_fields,
    )
    return True


@transaction.atomic
def delete_event_invitation(event: Event, invitation_id: UUID) -> bool:
    """Delete an EventInvitation. Returns True if deleted."""
    try:
        invitation = EventInvitation.objects.get(id=invitation_id, event=event)
        invitation.delete()
        return True
    except EventInvitation.DoesNotExist:
        return False


@transaction.atomic
def delete_pending_invitation(event: Event, invitation_id: UUID) -> bool:
    """Delete a PendingEventInvitation. Returns True if deleted."""
    try:
        invitation = PendingEventInvitation.objects.get(id=invitation_id, event=event)
        invitation.delete()
        return True
    except PendingEventInvitation.DoesNotExist:
        return False


def delete_invitation(event: Event, invitation_id: UUID, invitation_type: t.Literal["registered", "pending"]) -> bool:
    """Delete an invitation of the specified type. Returns True if deleted."""
    if invitation_type == "registered":
        return delete_event_invitation(event, invitation_id)
    return delete_pending_invitation(event, invitation_id)
