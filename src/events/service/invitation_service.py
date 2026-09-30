import contextlib
import typing as t
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

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

    Args:
        event: The event to invite people to.
        invitation_data: Emails, optional tiers and invitation fields.

    Returns:
        A summary of created invitations.

    Raises:
        HttpError: 400 when a tier id is unknown, or when the organization's daily budget of
            invitation emails to people without an account would be exceeded (#1035); nothing
            is created in either case.
    """
    # Validate tiers if provided
    tiers: list[TicketTier] = []
    if invitation_data.tier_ids:
        tiers = list(TicketTier.objects.filter(pk__in=invitation_data.tier_ids, event=event))
        if len(tiers) != len(invitation_data.tier_ids):
            found_ids = {t.pk for t in tiers}
            missing = [str(tid) for tid in invitation_data.tier_ids if tid not in found_ids]
            # Client-supplied tier ids: a bad one is addressable input, not an
            # internal invariant breach. A bare ``TicketTier.DoesNotExist`` is
            # unmapped and surfaced as a 500. Mirrors the other tier_ids guards
            # (ticket_service.reorder_tiers, membership.reorder_tiers).
            raise HttpError(400, str(_("Ticket tiers not found: %(ids)s")) % {"ids": ", ".join(missing)})

    _charge_pending_invitation_budget(event, {str(e).strip().lower() for e in invitation_data.emails})

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


# The counter must outlive the UTC day it names; the date in the key is what resets it.
_INVITE_CAP_COUNTER_TTL_SECONDS = 26 * 3600


def _charge_pending_invitation_budget(event: Event, emails: set[str]) -> None:
    """Charge the org's daily budget of invitation emails to people without a Revel account.

    Only addresses that would create a NEW ``PendingEventInvitation`` count: existing users get an
    ``EventInvitation`` instead, and an already-pending address only gets its row updated.
    Suppressed/opted-out addresses still count. The budget is never refunded (deleting and
    re-creating a pending invitation would otherwise resend cold mail for free).

    Raises:
        HttpError: 400 when the request would exceed the remaining budget; nothing is charged.
    """
    # ponytail: an error after the charge (same transaction) over-counts slightly, the safe side.
    # A per-org override would be a nullable ``Organization`` field read in place of the setting.
    cap = settings.PENDING_INVITATION_DAILY_CAP
    if not cap:
        return
    existing = set(RevelUser.objects.filter(email__in=emails).values_list("email", flat=True))
    pending = set(PendingEventInvitation.objects.filter(event=event, email__in=emails).values_list("email", flat=True))
    count = len(emails - existing - pending)
    if not count:
        return
    # Fail-closed by design: if Redis is unreachable this raises (500). Invitation emails go out via
    # Celery, whose broker is the same Redis, so nothing could be sent during the outage anyway.
    key = f"invite-cap:{event.organization_id}:{timezone.now():%Y%m%d}"
    cache.add(key, 0, timeout=_INVITE_CAP_COUNTER_TTL_SECONDS)
    try:
        spent = cache.incr(key, count)
    except ValueError:  # key expired/evicted between add() and incr(): start today's count afresh
        # add(), not set(): if a concurrent request recreated the key first, keep its charge.
        if cache.add(key, count, timeout=_INVITE_CAP_COUNTER_TTL_SECONDS):
            spent = count
        else:
            spent = cache.incr(key, count)
    if spent > cap:
        with contextlib.suppress(ValueError):  # key gone since incr(): nothing left to refund
            cache.decr(key, count)
        raise HttpError(
            400,
            str(
                _(
                    "This would email {count} people who don't have a Revel account yet, but your "
                    "organization can invite only {remaining} more today (limit {cap} per day, resets "
                    "at midnight UTC). Invite fewer people or try again tomorrow."
                )
            ).format(count=count, remaining=max(cap - (spent - count), 0), cap=cap),
        )


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
