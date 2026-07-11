"""Invitation and event token schemas."""

import typing as t
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, EmailStr, Field

from accounts.schema import MinimalRevelUserSchema
from common.schema import OneToOneFiftyString, StrippedString
from events import models

from .event import EventInListSchema
from .mixins import get_image_field_url
from .ticket import TicketTierSchema


class InvitationBaseSchema(Schema):
    waives_questionnaire: bool = False
    waives_purchase: bool = False
    overrides_max_attendees: bool = False
    waives_membership_required: bool = False
    waives_rsvp_deadline: bool = False
    waives_apply_deadline: bool = False
    custom_message: str | None = None


class InvitationSchema(InvitationBaseSchema):
    event: EventInListSchema
    tiers: list[TicketTierSchema] = Field(default_factory=list)
    user_id: UUID


class DirectInvitationCreateSchema(InvitationBaseSchema):
    """Schema for creating direct invitations to events.

    Note: Notifications are sent automatically via Django signals when invitations are created.
    """

    emails: list[EmailStr] = Field(..., min_length=1, description="List of email addresses to invite")
    tier_ids: list[UUID] = Field(default_factory=list, description="Ticket tiers to assign to invitations")


class DirectInvitationResponseSchema(Schema):
    """Response schema for direct invitation creation."""

    created_invitations: int = Field(..., description="Number of EventInvitation objects created")
    pending_invitations: int = Field(..., description="Number of PendingEventInvitation objects created")
    total_invited: int = Field(..., description="Total number of users invited")


class EventInvitationListSchema(Schema):
    """Schema for listing EventInvitation objects."""

    id: UUID
    user: MinimalRevelUserSchema
    tiers: list[TicketTierSchema] = Field(default_factory=list)
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


class MyEventInvitationSchema(Schema):
    """Schema for listing user's own EventInvitation objects with event details."""

    id: UUID
    event: EventInListSchema
    tiers: list[TicketTierSchema] = Field(default_factory=list)
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


class PendingEventInvitationListSchema(Schema):
    """Schema for listing PendingEventInvitation objects."""

    id: UUID
    email: str
    tiers: list[TicketTierSchema] = Field(default_factory=list)
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


class CombinedInvitationListSchema(Schema):
    """Schema combining both EventInvitation and PendingEventInvitation for listing."""

    id: UUID
    type: str = Field(..., description="'registered' for EventInvitation, 'pending' for PendingEventInvitation")
    user: MinimalRevelUserSchema | None = Field(None, description="User for registered invitations")
    email: str | None = Field(None, description="Email for pending invitations")
    tiers: list[TicketTierSchema] = Field(default_factory=list)
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


class EventJWTInvitationTier(Schema):
    name: OneToOneFiftyString
    description: StrippedString | None = None


class EventInvitationRequestCreateSchema(Schema):
    message: StrippedString | None = None


class EventInvitationRequestSchema(ModelSchema):
    user: MinimalRevelUserSchema
    event: EventInListSchema

    class Meta:
        model = models.EventInvitationRequest
        fields = ["id", "message", "status", "created_at"]


class EventInvitationRequestInternalSchema(EventInvitationRequestSchema):
    decided_by: MinimalRevelUserSchema | None = None


class EventTokenSchema(ModelSchema):
    ticket_tiers: list[TicketTierSchema] = Field(default_factory=list)
    # Additive, public-safe event details so the pre-claim token preview (unauthenticated)
    # can render which event the invitee is joining without a second lookup.
    # ``event`` stays a bare UUID to keep the event-admin token list contract unchanged.
    event_name: str
    event_slug: str
    organization_slug: str
    event_start: AwareDatetime
    event_cover_url: str | None = None

    class Meta:
        model = models.EventToken
        fields = [
            "id",
            "name",
            "issuer",
            "event",
            "expires_at",
            "uses",
            "max_uses",
            "grants_invitation",
            "invitation_payload",
            "created_at",
        ]

    @staticmethod
    def resolve_event_name(obj: models.EventToken) -> str:
        """Return the token's event name."""
        return obj.event.name

    @staticmethod
    def resolve_event_slug(obj: models.EventToken) -> str:
        """Return the token's event slug."""
        return obj.event.slug

    @staticmethod
    def resolve_organization_slug(obj: models.EventToken) -> str:
        """Return the slug of the event's organization (for FE navigation)."""
        return obj.event.organization.slug

    @staticmethod
    def resolve_event_start(obj: models.EventToken) -> AwareDatetime:
        """Return the event start time."""
        return obj.event.start

    @staticmethod
    def resolve_event_cover_url(obj: models.EventToken) -> str | None:
        """Return the event's social cover-art URL, if any (public, unsigned)."""
        return get_image_field_url(obj.event, "cover_art_social")


class EventTokenRejectionSchema(Schema):
    """Returned with 410 Gone when an event token exists but is no longer servable.

    Lets the unauthenticated pre-claim page tell "expired" from "used up" and still
    render which event the dead link pointed at.
    """

    message: str
    reason: t.Literal["expired", "used_up"]
    event_name: str
    event_slug: str
    organization_slug: str


class EventTokenBaseSchema(Schema):
    name: OneToOneFiftyString | None = None
    max_uses: int = 1
    grants_invitation: bool = False
    invitation_payload: InvitationBaseSchema | None = None
    ticket_tier_ids: list[UUID] = Field(default_factory=list, description="Ticket tiers to assign when claiming")


class EventTokenCreateSchema(EventTokenBaseSchema):
    duration: int = 24 * 60


class EventTokenUpdateSchema(EventTokenBaseSchema):
    expires_at: AwareDatetime | None = None
