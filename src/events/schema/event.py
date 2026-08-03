"""Event-related schemas."""

import typing as t
from uuid import UUID

from annotated_types import Len
from django.utils.translation import gettext as _
from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from accounts.models import RevelUser
from common.schema import (
    OneToOneFiftyString,
    OneToSixtyFourString,
    ProfilePictureSchemaMixin,
    StrippedString,
    viewer_from_context,
)
from events.models import Event, ResourceVisibility
from events.utils.schedule import EventScheduleSession
from events.utils.visibility_settings import EventVisibilitySettings
from geo.schema import CitySchema

from .event_series import MinimalEventSeriesSchema
from .mixins import CityEditMixin, LogoCoverArtThumbnailMixin, TaggableSchemaMixin
from .organization import MinimalOrganizationSchema
from .venue import VenueSchema

# Re-export the pydantic settings model as the API schema (single source of truth),
# mirroring EventScheduleSessionSchema below and RefundPolicySchema in schema/ticket.py.
EventVisibilitySettingsSchema = EventVisibilitySettings


class SeriesPassLinkInputSchema(Schema):
    """One (series pass, tier) pair to link when creating/editing an event."""

    series_pass_id: UUID
    tier_id: UUID


class EventEditSchema(CityEditMixin):
    name: OneToOneFiftyString | None = None
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
    is_open_ended: bool = False
    rsvp_before: AwareDatetime | None = Field(None, description="RSVP deadline for events that do not require tickets")
    check_in_starts_at: AwareDatetime | None = Field(None, description="When check-in opens for this event")
    check_in_ends_at: AwareDatetime | None = Field(None, description="When check-in closes for this event")
    requires_full_profile: bool = False
    event_series_id: UUID | None = None
    venue_id: UUID | None = None
    potluck_open: bool = False
    accept_invitation_requests: bool = False
    accept_rsvp_notes: bool = False
    apply_before: AwareDatetime | None = Field(
        None, description="Deadline for submitting invitation requests or questionnaires"
    )
    can_attend_without_login: bool = False
    # Defaults to True, matching the model, so clients that omit it never
    # silently relax the holder-name requirement on an existing event.
    require_ticket_names: bool = True
    # Merged, not replaced, on edit: sending one toggle leaves the others at
    # their stored values (see ``build_visibility_settings_update``). Omitting a
    # toggle means "no change", exactly like omitting any other field here.
    visibility_settings: EventVisibilitySettingsSchema = Field(default_factory=EventVisibilitySettingsSchema)
    series_pass_links: list[SeriesPassLinkInputSchema] | None = None


class EventCreateSchema(EventEditSchema):
    name: OneToOneFiftyString
    start: AwareDatetime
    requires_ticket: bool = False


# Re-export the pydantic session model as the API schema (single source of truth),
# mirroring RefundPolicySchema = RefundPolicy in schema/ticket.py.
EventScheduleSessionSchema = EventScheduleSession


class EventScheduleUpdateSchema(Schema):
    """Full-array replace payload for an event's schedule."""

    sessions: t.Annotated[list[EventScheduleSessionSchema], Len(max_length=200)] = []


class EventDuplicateSchema(Schema):
    """Schema for duplicating an event."""

    name: OneToOneFiftyString
    start: AwareDatetime


# Slug must be lowercase alphanumeric with hyphens, 1-255 chars
SlugString = t.Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class EventEditSlugSchema(Schema):
    """Schema for editing an event's slug."""

    slug: SlugString


class EventStatusUpdatePayload(Schema):
    """Optional body for the update-status endpoint.

    ``cancellation_reason`` is honored only when transitioning to CANCELLED;
    it is ignored for every other target status.
    """

    cancellation_reason: StrippedString | None = Field(default=None, max_length=1000)


class EventBaseSchema(TaggableSchemaMixin, LogoCoverArtThumbnailMixin):
    id: UUID
    event_type: Event.EventType
    visibility: Event.Visibility
    organization: MinimalOrganizationSchema
    status: Event.EventStatus
    event_series: MinimalEventSeriesSchema | None = None
    venue: VenueSchema | None = None
    name: str
    slug: str
    description: str | None = None
    invitation_message: str | None = None
    max_attendees: int | None = 0
    max_tickets_per_user: int | None = None
    waitlist_open: bool | None = None
    start: AwareDatetime
    end: AwareDatetime
    is_open_ended: bool = False
    timezone: str
    rsvp_before: AwareDatetime | None = None
    logo: str | None = None
    cover_art: str | None = None
    requires_ticket: bool
    requires_full_profile: bool
    potluck_open: bool
    attendee_count: int | None = None
    is_full: bool = False
    visibility_settings: EventVisibilitySettingsSchema = Field(default_factory=EventVisibilitySettingsSchema)
    accept_invitation_requests: bool
    accept_rsvp_notes: bool
    apply_before: AwareDatetime | None = None
    can_attend_without_login: bool
    require_ticket_names: bool
    # Recurring-series fields. Included in the base schema so list views can
    # show series context (e.g. occurrence position). ``is_template`` is
    # always ``False`` in user-facing responses because ``Event.objects.for_user()``
    # filters ``is_template=False``; it is kept here for schema completeness
    # and superuser/admin tooling that may bypass ``for_user()``.
    is_template: bool = False
    is_modified: bool = False
    occurrence_index: int | None = None
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None
    seats_held: int | None = 0
    is_bookmarked: bool = False
    cancellation_reason: str | None = None

    @staticmethod
    def resolve_timezone(obj: "Event") -> str:
        """Expose the event's IANA timezone (e.g. ``Europe/Vienna``).

        Mirrors the timezone the backend uses to format emails/notifications
        (``get_event_timezone``), derived from the event's city with a UTC
        fallback. The frontend renders the abbreviation/offset from this.
        """
        from events.utils import get_event_timezone

        return str(get_event_timezone(obj))

    @staticmethod
    def resolve_cancellation_reason(obj: "Event", context: t.Any) -> str | None:
        """Surface the organizer's cancellation reason to attendees only.

        The DB stores an empty string by default; the API contract prefers
        ``null`` so the frontend only renders the reason when one was set. The
        reason is only disclosed to users who were actually attending (ticket or
        confirmed RSVP) plus the event's staff/owners — mirroring address
        visibility. Empty reasons short-circuit before any access query, so the
        common non-cancelled list case incurs no extra lookups.
        """
        if not obj.cancellation_reason:
            return None
        user = context["request"].user
        if obj.can_user_see_cancellation_reason(user):
            return obj.cancellation_reason
        return None

    @staticmethod
    def resolve_is_bookmarked(obj: "Event", context: t.Any) -> bool:
        """Whether the current user has bookmarked this event.

        Reads the ``user_has_bookmarked`` annotation when present (set by
        ``EventQuerySet.with_user_bookmark`` on list/detail querysets). Falls
        back to a direct lookup for callers that haven't annotated.
        """
        annotated = getattr(obj, "user_has_bookmarked", None)
        if annotated is not None:
            return bool(annotated)

        from events.models import EventBookmark

        user = context["request"].user
        return user.is_authenticated and EventBookmark.objects.filter(user=user, event=obj).exists()

    @staticmethod
    def resolve_attendee_count(obj: "Event", context: t.Any) -> int | None:
        """Disclose the confirmed-attendee count only when the event allows it.

        Returns ``None`` when ``visibility_settings.show_attendee_count`` is off
        and the viewer is not privileged (org owner/staff, Django staff). The
        always-public ``is_full`` boolean covers the sold-out case for the
        frontend without disclosing the exact number.
        """
        user = viewer_from_context(context)
        return obj.attendee_count if obj.can_user_see_attendee_count(user) else None

    @staticmethod
    def resolve_max_attendees(obj: "Event", context: t.Any) -> int | None:
        """Disclose the configured capacity only when the event allows it.

        Returns ``None`` when ``visibility_settings.show_capacity`` is off and
        the viewer is not privileged. ``0`` still means "uncapped"; ``None`` now
        means "not disclosed", so clients must distinguish the two.
        """
        user = viewer_from_context(context)
        return obj.max_attendees if obj.can_user_see_capacity(user) else None

    @staticmethod
    def resolve_seats_held(obj: "Event", context: t.Any) -> int | None:
        """Count pending unexpired non-cutoff waitlist offers (reserved seats).

        Gated by ``visibility_settings.show_capacity`` — held seats are a
        capacity figure and combining them with ``max_attendees`` would leak
        spots-left. Returns ``None`` when the viewer may not see capacity; the
        gate short-circuits before any query.

        Reads from the ``pending_waitlist_offer_count`` annotation when available
        (set by ``EligibilityService.__init__`` and any queryset that opts into it).
        Falls back to a direct COUNT query for callers that haven't annotated.
        """
        if not obj.can_user_see_capacity(viewer_from_context(context)):
            return None

        annotated = getattr(obj, "pending_waitlist_offer_count", None)
        if annotated is not None:
            return int(annotated)

        from django.utils import timezone

        from events.models import WaitlistOffer

        return WaitlistOffer.objects.filter(
            event=obj,
            status=WaitlistOffer.WaitlistOfferStatus.PENDING,
            expires_at__gt=timezone.now(),
            is_cutoff_batch=False,
        ).count()


class EventInListSchema(EventBaseSchema):
    city: CitySchema | None = None


class EventDetailSchema(EventBaseSchema):
    city: CitySchema | None = None
    address: str | None = None
    location_maps_url: str | None = None
    location_maps_embed: str | None = None
    check_in_starts_at: AwareDatetime | None = None
    check_in_ends_at: AwareDatetime | None = None
    schedule: list[EventScheduleSessionSchema] = []

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
        return visibility_messages.get(obj.visibility_flags.address_visibility)

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


class MinimalEventSchema(LogoCoverArtThumbnailMixin):
    id: UUID
    slug: str
    name: str
    start: AwareDatetime
    end: AwareDatetime
    is_open_ended: bool = False
    logo: str | None = None
    cover_art: str | None = None
    venue: VenueSchema | None = None


class TagUpdateSchema(BaseModel):
    tags: list[OneToSixtyFourString] = Field(..., description="A list of tag names to add or remove.")


class AttendeeSchema(ProfilePictureSchemaMixin, ModelSchema):
    display_name: str
    bio: str

    class Meta:
        model = RevelUser
        fields = ["preferred_name", "pronouns", "first_name", "last_name"]
