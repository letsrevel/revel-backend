import typing as t
from uuid import UUID

from django.db import transaction

from accounts.models import RevelUser
from events.models import Event, EventInvitation, PendingEventInvitation, TicketTier
from events.schema import DirectInvitationCreateSchema
from events.utils import get_invitation_message


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
    # Validate tiers if provided
    tiers: list[TicketTier] = []
    if invitation_data.tier_ids:
        tiers = list(TicketTier.objects.filter(pk__in=invitation_data.tier_ids, event=event))
        if len(tiers) != len(invitation_data.tier_ids):
            found_ids = {t.pk for t in tiers}
            missing = [str(tid) for tid in invitation_data.tier_ids if tid not in found_ids]
            raise TicketTier.DoesNotExist(f"Ticket tiers not found: {', '.join(missing)}")

    invitation_fields = _get_invitation_fields(invitation_data)
    created_invitations = 0
    pending_invitations = 0

    for email_str in invitation_data.emails:
        email = str(email_str).strip().lower()

        if user := RevelUser.objects.filter(email=email).first():
            # User exists - create EventInvitation
            fields = _with_default_message(invitation_fields, user.get_display_name(), event)
            if _create_or_update_event_invitation(event, user, fields, tiers):
                created_invitations += 1
        else:
            fields = _with_default_message(invitation_fields, email, event)
            if _create_or_update_pending_invitation(event, email, fields, tiers):
                pending_invitations += 1

    # Note: Notifications are sent automatically via Django signals
    # (see notifications/signals/invitation.py)

    return {
        "created_invitations": created_invitations,
        "pending_invitations": pending_invitations,
        "total_invited": created_invitations + pending_invitations,
    }


def _with_default_message(invitation_fields: dict[str, t.Any], display_name: str, event: Event) -> dict[str, t.Any]:
    """Return a copy of invitation_fields with custom_message filled from the event default if empty."""
    if invitation_fields.get("custom_message"):
        return invitation_fields
    return {**invitation_fields, "custom_message": get_invitation_message(display_name, event)}


def _get_invitation_fields(invitation_data: DirectInvitationCreateSchema) -> dict[str, t.Any]:
    """Extract invitation fields from schema."""
    return {
        "waives_questionnaire": invitation_data.waives_questionnaire,
        "waives_purchase": invitation_data.waives_purchase,
        "overrides_max_attendees": invitation_data.overrides_max_attendees,
        "waives_membership_required": invitation_data.waives_membership_required,
        "waives_rsvp_deadline": invitation_data.waives_rsvp_deadline,
        "waives_apply_deadline": invitation_data.waives_apply_deadline,
        "custom_message": invitation_data.custom_message,
    }


def _create_or_update_event_invitation(
    event: Event, user: RevelUser, invitation_fields: dict[str, t.Any], tiers: list[TicketTier]
) -> bool:
    """Create or update an EventInvitation for an existing user. Returns True if created/updated."""
    invitation, _ = EventInvitation.objects.update_or_create(
        event=event,
        user=user,
        defaults=invitation_fields,
    )
    invitation.tiers.set(tiers)
    return True


def _create_or_update_pending_invitation(
    event: Event, email: str, invitation_fields: dict[str, t.Any], tiers: list[TicketTier]
) -> bool:
    """Create or update a PendingEventInvitation for a non-existing user. Returns True if created/updated."""
    invitation, _ = PendingEventInvitation.objects.update_or_create(
        event=event,
        email=email,
        defaults=invitation_fields,
    )
    invitation.tiers.set(tiers)
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
