"""Invitation and event token schemas."""

from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, EmailStr, Field

from accounts.schema import MinimalRevelUserSchema
from common.schema import OneToOneFiftyString, StrippedString
from events import models

from .event import EventInListSchema
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
    tier: TicketTierSchema | None = None
    user_id: UUID


class DirectInvitationCreateSchema(InvitationBaseSchema):
    """Schema for creating direct invitations to events.

    Note: Notifications are sent automatically via Django signals when invitations are created.
    """

    emails: list[EmailStr] = Field(..., min_length=1, description="List of email addresses to invite")
    tier_id: UUID | None = Field(None, description="Ticket tier to assign to invitations")


class DirectInvitationResponseSchema(Schema):
    """Response schema for direct invitation creation."""

    created_invitations: int = Field(..., description="Number of EventInvitation objects created")
    pending_invitations: int = Field(..., description="Number of PendingEventInvitation objects created")
    total_invited: int = Field(..., description="Total number of users invited")


class EventInvitationListSchema(Schema):
    """Schema for listing EventInvitation objects."""

    id: UUID
    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None
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
    tier: TicketTierSchema | None = None
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
    tier: TicketTierSchema | None = None
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
    tier: TicketTierSchema | None = None
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
    class Meta:
        model = models.EventToken
        fields = "__all__"


class EventTokenBaseSchema(Schema):
    name: OneToOneFiftyString | None = None
    max_uses: int = 1
    grants_invitation: bool = False
    invitation_payload: InvitationBaseSchema | None = None
    ticket_tier_id: UUID | None = None


class EventTokenCreateSchema(EventTokenBaseSchema):
    duration: int = 24 * 60


class EventTokenUpdateSchema(EventTokenBaseSchema):
    expires_at: AwareDatetime | None = None
