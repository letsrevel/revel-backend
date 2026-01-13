"""Event token management for invitations and access control."""

import typing as t
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventToken


def create_event_token(
    *,
    event: Event,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    invitation_payload: dict[str, t.Any] | None = None,
    ticket_tier_id: UUID | None = None,
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
        ticket_tier_id: Optional ticket tier to associate with invitations.
        name: Optional name/label for the token.
        grants_invitation: Whether claiming this token grants an invitation.
        max_uses: Maximum number of times this token can be used (0 = unlimited).

    Returns:
        The created EventToken.
    """
    duration = timedelta(minutes=duration) if isinstance(duration, int) else duration
    return EventToken.objects.create(
        name=name,
        issuer=issuer,
        event=event,
        expires_at=timezone.now() + duration,
        max_uses=max_uses,
        ticket_tier_id=ticket_tier_id,
        grants_invitation=grants_invitation,
        invitation_payload=invitation_payload,
    )


def get_event_token(token: str) -> EventToken | None:
    """Retrieve an EventToken by its ID.

    Args:
        token: The token ID (UUID as string).

    Returns:
        The EventToken if found and not expired, None otherwise.
    """
    return (
        EventToken.objects.select_related("event")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )


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
    event_token = get_event_token(token)
    if event_token is None:
        return None
    if not event_token.grants_invitation:
        return None
    if event_token.max_uses and event_token.uses >= event_token.max_uses:
        return None
    # Warning: do not save the event_token object directly here.
    # Use update() after get_or_create to avoid race conditions.
    invitation, created = EventInvitation.objects.get_or_create(
        event=event_token.event,
        user=user,
        defaults={
            "tier_id": event_token.ticket_tier_id,
            **(event_token.invitation_payload or {}),
        },
    )
    if created:
        EventToken.objects.filter(pk=event_token.pk).update(uses=F("uses") + 1)
    return invitation
