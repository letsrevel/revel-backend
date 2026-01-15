"""RSVP and waitlist schemas."""

from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field

from accounts.schema import MinimalRevelUserSchema
from events import models
from events.models import EventRSVP

from .event import MinimalEventSchema
from .organization import MinimalOrganizationMemberSchema
from .ticket import UserTicketSchema


class EventRSVPSchema(ModelSchema):
    event_id: UUID
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["status"]


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
        fields = ["id", "status", "created_at", "updated_at"]

    @staticmethod
    def resolve_membership(obj: EventRSVP) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None


class RSVPCreateSchema(Schema):
    """Schema for creating an RSVP on behalf of a user."""

    user_id: UUID
    status: EventRSVP.RsvpStatus


class RSVPUpdateSchema(Schema):
    """Schema for updating an RSVP."""

    status: EventRSVP.RsvpStatus


# Waitlist Admin Schemas


class WaitlistEntrySchema(ModelSchema):
    """Schema for waitlist entry details in admin views."""

    id: UUID
    event_id: UUID
    user: MinimalRevelUserSchema
    created_at: AwareDatetime
    updated_at: AwareDatetime

    class Meta:
        model = models.EventWaitList
        fields = ["id", "created_at", "updated_at"]


class UserRSVPSchema(ModelSchema):
    """Schema for user's own RSVPs with event details."""

    event: MinimalEventSchema
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "created_at", "updated_at"]


class EventUserStatusResponse(Schema):
    """Response for user's status at an event.

    This is a unified response that includes:
    - Tickets: List of user's tickets for this event (if any)
    - RSVP: User's RSVP status (for non-ticketed events)
    - Eligibility: Whether user can purchase tickets and why not
    - Purchase limits: How many more tickets can be purchased
    - Feedback questionnaires: Available after event ends for attendees
    """

    tickets: list[UserTicketSchema] = Field(default_factory=list)
    rsvp: EventRSVPSchema | None = None
    can_purchase_more: bool = True
    remaining_tickets: int | None = None  # None = unlimited
    feedback_questionnaires: list[UUID] = Field(default_factory=list)
