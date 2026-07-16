"""RSVP and waitlist schemas."""

import typing as t
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field, StringConstraints

from accounts.schema import MinimalRevelUserSchema
from events import models
from events.models import EventRSVP, WaitlistOffer

from .event import MinimalEventSchema
from .organization import MinimalOrganizationMemberSchema
from .ticket import GuestUserDataSchema, UserTicketSchema
from .waitlist import WaitlistOfferSchema

RSVPNoteField = t.Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class RSVPNoteSchema(Schema):
    """Optional JSON body for the RSVP endpoint."""

    note: RSVPNoteField = ""


class GuestRSVPRequestSchema(GuestUserDataSchema):
    """Guest RSVP payload: guest identity plus optional note."""

    note: RSVPNoteField = ""


class EventRSVPSchema(ModelSchema):
    event_id: UUID
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["status", "note"]


# RSVP Admin Schemas


class RSVPDetailSchema(ModelSchema):
    """Schema for RSVP details in admin views."""

    id: UUID
    event_id: UUID
    user: MinimalRevelUserSchema
    status: EventRSVP.RsvpStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    membership: MinimalOrganizationMemberSchema | None = None

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "note", "created_at", "updated_at"]

    @staticmethod
    def resolve_membership(obj: EventRSVP) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None


class RSVPCreateSchema(Schema):
    """Schema for creating an RSVP on behalf of a user."""

    user_id: UUID
    status: EventRSVP.RsvpStatus
    note: RSVPNoteField = ""


class RSVPUpdateSchema(Schema):
    """Schema for updating an RSVP."""

    status: EventRSVP.RsvpStatus
    note: RSVPNoteField = ""


# Waitlist Admin Schemas


class WaitlistEntrySchema(ModelSchema):
    """Schema for waitlist entry details in admin views."""

    id: UUID
    event_id: UUID
    user: MinimalRevelUserSchema
    created_at: AwareDatetime
    updated_at: AwareDatetime
    current_offer: WaitlistOfferSchema | None = None

    class Meta:
        model = models.EventWaitList
        fields = ["id", "created_at", "updated_at"]

    @staticmethod
    def resolve_current_offer(obj: "models.EventWaitList") -> WaitlistOffer | None:
        """Return the most recent live PENDING offer for this (event, user), if any.

        Filters out offers whose ``expires_at`` is in the past (zombie PENDING
        rows the hourly sweeper hasn't transitioned to EXPIRED yet) so the
        admin UI never surfaces an offer the user can no longer claim.

        Performs a single DB hit per row. Acceptable for the paginated admin
        endpoint (page_size=20). Optimize via prefetch only if the cost becomes
        material.
        """
        from django.utils import timezone

        return (
            WaitlistOffer.objects.select_related("user")
            .filter(
                event_id=obj.event_id,
                user_id=obj.user_id,
                status=WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )


class UserRSVPSchema(ModelSchema):
    """Schema for user's own RSVPs with event details."""

    event: MinimalEventSchema
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "note", "created_at", "updated_at"]


class TierRemainingTicketsSchema(Schema):
    """Remaining tickets for a specific tier.

    Attributes:
        tier_id: The tier's UUID.
        remaining: How many more tickets the user can purchase (None = unlimited).
        sold_out: Whether the tier itself is sold out (no inventory remaining).
    """

    tier_id: UUID
    remaining: int | None = None  # None = unlimited
    sold_out: bool = False
    can_purchase: bool = True


class EventUserStatusResponse(Schema):
    """Response for user's status at an event.

    This is a unified response that includes:
    - Tickets: List of user's tickets for this event (if any)
    - RSVP: User's RSVP status (for non-ticketed events)
    - Eligibility: Whether user can purchase tickets and why not
    - Purchase limits: Per-tier remaining tickets accounting for user, tier, and event capacity
    - Feedback questionnaires: Available after event ends for attendees
    """

    tickets: list[UserTicketSchema] = Field(default_factory=list)
    rsvp: EventRSVPSchema | None = None
    can_purchase_more: bool = True
    remaining_tickets: list[TierRemainingTicketsSchema] = Field(default_factory=list)
    feedback_questionnaires: list[UUID] = Field(default_factory=list)
