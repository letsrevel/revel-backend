"""Event-related schemas."""

import typing as t
from uuid import UUID

from django.utils.translation import gettext as _
from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from accounts.models import RevelUser
from common.schema import OneToOneFiftyString, OneToSixtyFourString, StrippedString
from events.models import Event, ResourceVisibility
from geo.schema import CitySchema

from .event_series import MinimalEventSeriesSchema
from .mixins import CityEditMixin, TaggableSchemaMixin
from .organization import MinimalOrganizationSchema
from .venue import VenueSchema


class EventEditSchema(CityEditMixin):
    name: OneToOneFiftyString | None = None
    address_visibility: ResourceVisibility = ResourceVisibility.PUBLIC
    description: StrippedString | None = None
    event_type: Event.EventType | None = None
    status: Event.EventStatus = Event.EventStatus.DRAFT
    visibility: Event.Visibility | None = None
    invitation_message: StrippedString | None = Field(None, description="Invitation message")
    max_attendees: int = 0
    max_tickets_per_user: int | None = Field(None, description="Max tickets per user (null = unlimited)")
    waitlist_open: bool = False
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    rsvp_before: AwareDatetime | None = Field(None, description="RSVP deadline for events that do not require tickets")
    check_in_starts_at: AwareDatetime | None = Field(None, description="When check-in opens for this event")
    check_in_ends_at: AwareDatetime | None = Field(None, description="When check-in closes for this event")
    event_series_id: UUID | None = None
    venue_id: UUID | None = None
    potluck_open: bool = False
    accept_invitation_requests: bool = False
    apply_before: AwareDatetime | None = Field(
        None, description="Deadline for submitting invitation requests or questionnaires"
    )
    can_attend_without_login: bool = False


class EventCreateSchema(EventEditSchema):
    name: OneToOneFiftyString
    start: AwareDatetime
    requires_ticket: bool = False


class EventDuplicateSchema(Schema):
    """Schema for duplicating an event."""

    name: OneToOneFiftyString
    start: AwareDatetime


# Slug must be lowercase alphanumeric with hyphens, 1-255 chars
SlugString = t.Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class EventEditSlugSchema(Schema):
    """Schema for editing an event's slug."""

    slug: SlugString


class EventBaseSchema(TaggableSchemaMixin):
    id: UUID
    event_type: Event.EventType
    visibility: Event.Visibility
    address_visibility: ResourceVisibility = ResourceVisibility.PUBLIC
    organization: MinimalOrganizationSchema
    status: Event.EventStatus
    event_series: MinimalEventSeriesSchema | None = None
    venue: VenueSchema | None = None
    name: str
    slug: str
    description: str | None = None
    invitation_message: str | None = None
    max_attendees: int = 0
    max_tickets_per_user: int | None = None
    waitlist_open: bool | None = None
    start: AwareDatetime
    end: AwareDatetime
    rsvp_before: AwareDatetime | None = None
    logo: str | None = None
    cover_art: str | None = None
    requires_ticket: bool
    potluck_open: bool
    attendee_count: int
    accept_invitation_requests: bool
    apply_before: AwareDatetime | None = None
    can_attend_without_login: bool
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None


class EventInListSchema(EventBaseSchema):
    city: CitySchema | None = None


class EventDetailSchema(EventBaseSchema):
    city: CitySchema | None = None
    address: str | None = None
    location_maps_url: str | None = None
    location_maps_embed: str | None = None

    @staticmethod
    def resolve_address(obj: Event, context: t.Any) -> str | None:
        """Conditionally return address based on address_visibility setting.

        If the user cannot see the address, returns an explanatory message
        about who can see it based on the address_visibility setting.
        """
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.address

        # Return explanation based on visibility setting
        visibility_messages: dict[str, str] = {
            ResourceVisibility.PRIVATE: _("Address visible to invited guests only"),
            ResourceVisibility.MEMBERS_ONLY: _("Address visible to organization members only"),
            ResourceVisibility.STAFF_ONLY: _("Address visible to staff only"),
            ResourceVisibility.ATTENDEES_ONLY: _("Address visible to attendees only"),
        }
        return visibility_messages.get(obj.address_visibility)

    @staticmethod
    def resolve_location_maps_url(obj: Event, context: t.Any) -> str | None:
        """Return maps URL only if user can see the address."""
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.location_maps_url
        return None

    @staticmethod
    def resolve_location_maps_embed(obj: Event, context: t.Any) -> str | None:
        """Return maps embed URL only if user can see the address."""
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.location_maps_embed
        return None


class MinimalEventSchema(Schema):
    id: UUID
    slug: str
    name: str
    start: AwareDatetime
    end: AwareDatetime
    logo: str | None = None
    cover_art: str | None = None
    venue: VenueSchema | None = None


class TagUpdateSchema(BaseModel):
    tags: list[OneToSixtyFourString] = Field(..., description="A list of tag names to add or remove.")


class AttendeeSchema(ModelSchema):
    display_name: str

    class Meta:
        model = RevelUser
        fields = ["preferred_name", "pronouns", "first_name", "last_name"]
