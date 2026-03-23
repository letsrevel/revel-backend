"""Event token management for invitations and access control."""

import typing as t
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventToken, TicketTier
from events.utils import get_invitation_message


def create_event_token(
    *,
    event: Event,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    invitation_payload: dict[str, t.Any] | None = None,
    ticket_tier_ids: list[UUID] | None = None,
    name: str | None = None,
    grants_invitation: bool = False,
    max_uses: int = 0,
) -> EventToken:
    """Create a temporary event token.

    Args:
        event: The event this token is for.
        issuer: The user creating the token.
        duration: Token validity duration (timedelta or minutes as int).
        invitation_payload: Additional data to include in invitations claimed with this token.
        ticket_tier_ids: Optional ticket tier IDs to associate with invitations.
        name: Optional name/label for the token.
        grants_invitation: Whether claiming this token grants an invitation.
        max_uses: Maximum number of times this token can be used (0 = unlimited).

    Returns:
        The created EventToken.
    """
    duration = timedelta(minutes=duration) if isinstance(duration, int) else duration
    token = EventToken.objects.create(
        name=name,
        issuer=issuer,
        event=event,
        expires_at=timezone.now() + duration,
        max_uses=max_uses,
        grants_invitation=grants_invitation,
        invitation_payload=invitation_payload,
    )
    if ticket_tier_ids:
        tiers = TicketTier.objects.filter(pk__in=ticket_tier_ids, event=event)
        token.ticket_tiers.set(tiers)
    # Refetch with prefetch to ensure M2M is loaded for serialization
    return EventToken.objects.select_related("event").prefetch_related("ticket_tiers").get(pk=token.pk)


class TokenRejection(t.NamedTuple):
    """Why a token was rejected, plus the event it belongs to."""

    reason: t.Literal["expired", "used_up"]
    event_id: UUID


def get_event_token(token: str) -> EventToken | None:
    """Retrieve an EventToken by its ID.

    Returns the token only if it is still valid: not expired and not
    exhausted (``uses < max_uses``, or ``max_uses == 0`` for unlimited).

    Args:
        token: The token ID string.

    Returns:
        The EventToken if found and still valid, None otherwise.
    """
    return (
        EventToken.objects.select_related("event")
        .prefetch_related("ticket_tiers")
        .filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            Q(max_uses=0) | Q(uses__lt=F("max_uses")),
            pk=token,
        )
        .first()
    )


def get_token_rejection_reason(token: str) -> TokenRejection | None:
    """Diagnose why a token is no longer valid.

    Called only after get_event_token() returned None to distinguish
    "token doesn't exist" from "token expired / used up".

    Returns:
        TokenRejection with the reason and event_id, or None if the token
        simply doesn't exist (genuine 404).
    """
    event_token = EventToken.objects.only("expires_at", "uses", "max_uses", "event_id").filter(pk=token).first()
    if event_token is None:
        return None
    if event_token.expires_at and event_token.expires_at <= timezone.now():
        return TokenRejection(reason="expired", event_id=event_token.event_id)
    if event_token.max_uses and event_token.uses >= event_token.max_uses:
        return TokenRejection(reason="used_up", event_id=event_token.event_id)
    return None


@transaction.atomic
def claim_invitation(user: RevelUser, token: str) -> EventInvitation | None:
    """Claim an invitation using an event token.

    Args:
        user: The user claiming the invitation.
        token: The token ID to claim.

    Returns:
        The EventInvitation if successfully claimed, None if token is invalid,
        doesn't grant invitations, or has reached max uses.
    """
    # Lock the token row to prevent concurrent requests from exceeding max_uses.
    event_token = (
        EventToken.objects.select_for_update()
        .select_related("event")
        .prefetch_related("ticket_tiers")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )
    if event_token is None:
        return None
    if not event_token.grants_invitation:
        return None
    if event_token.max_uses and event_token.uses >= event_token.max_uses:
        return None
    # Warning: do not save the event_token object directly here.
    # Use update() after get_or_create to avoid race conditions.
    defaults = {
        **(event_token.invitation_payload or {}),
    }
    if not defaults.get("custom_message"):
        defaults["custom_message"] = get_invitation_message(user.get_display_name(), event_token.event)
    invitation, created = EventInvitation.objects.get_or_create(
        event=event_token.event,
        user=user,
        defaults=defaults,
    )
    if created:
        EventToken.objects.filter(pk=event_token.pk).update(uses=F("uses") + 1)
        # Copy tier links from token to invitation
        token_tiers = event_token.ticket_tiers.all()
        if token_tiers:
            invitation.tiers.set(token_tiers)
    return invitation
