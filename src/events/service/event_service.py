import typing as t
from datetime import timedelta
from uuid import UUID

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventToken,
    TicketTier,
)
from events.models.mixins import LocationMixin
from events.schema import InvitationBaseSchema

T = t.TypeVar("T", bound=LocationMixin)


def order_by_distance(point: Point | None, queryset: QuerySet[T]) -> QuerySet[T]:
    """Get cities by ip."""
    if point is None:
        return queryset

    return queryset.annotate(  # type: ignore[no-any-return]
        distance=Distance("location", point),
    ).order_by("distance")


def create_event_token(
    *,
    event: Event,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    invitation: InvitationBaseSchema | None = None,
    invitation_tier_id: UUID | None = None,
    name: str | None = None,
    max_uses: int = 0,
) -> EventToken:
    """Get a temporary JWT.

    This will need to be used by a user in combination with their OTP code to obtain a valid JWT.
    """
    duration = timedelta(minutes=duration) if isinstance(duration, int) else duration
    return EventToken.objects.create(
        name=name,
        issuer=issuer,
        event=event,
        expires_at=timezone.now() + duration,
        max_uses=max_uses,
        invitation_tier_id=invitation_tier_id,
        invitation_payload=invitation.model_dump(mode="json") if invitation is not None else None,
    )


def get_event_token(token: str) -> EventToken | None:
    """Retrieves an EventToken from a JWT."""
    return (
        EventToken.objects.select_related("event")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )


@transaction.atomic
def claim_invitation(user: RevelUser, token: str) -> EventInvitation | None:
    """Claim an invitation given an Event JWT."""
    event_token = get_event_token(token)
    if event_token is None:
        return None
    if not event_token.grants_invitation:
        return None
    if event_token.max_uses and event_token.uses >= event_token.max_uses:
        return None
    # warning: do not save the event_token object now. If pop() is removed get_or_create will fail)
    invitation, created = EventInvitation.objects.get_or_create(
        event=event_token.event,
        user=user,
        defaults={
            "tier": event_token.invitation_tier,
            **(event_token.invitation_payload or {}),
        },
    )
    if created:
        EventToken.objects.filter(pk=event_token.pk).update(uses=F("uses") + 1)
    return invitation


@transaction.atomic
def approve_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser, tier: TicketTier | None = None
) -> EventInvitationRequest:
    """Approve an invitation request."""
    invitation_request.status = EventInvitationRequest.Status.APPROVED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status"])
    EventInvitation.objects.create(event=invitation_request.event, user=invitation_request.user, tier=tier)
    return invitation_request


def reject_invitation_request(
    invitation_request: EventInvitationRequest, decided_by: RevelUser
) -> EventInvitationRequest:
    """Reject an invitation request."""
    invitation_request.status = EventInvitationRequest.Status.REJECTED
    invitation_request.decided_by = decided_by
    invitation_request.save(update_fields=["status", "decided_by"])
    return invitation_request
