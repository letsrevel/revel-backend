"""Schemas for recurring event operations."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field

from common.schema import OneToOneFiftyString, StrippedString
from events.models import Event, ResourceVisibility
from events.models.event_series import MAX_GENERATION_WINDOW_WEEKS

from .event import EventCreateSchema, MinimalEventSchema
from .recurrence_rule import RecurrenceRuleCreateSchema, RecurrenceRuleSchema, RecurrenceRuleUpdateSchema


class RecurringEventCreateSchema(Schema):
    """Schema for creating a recurring event (series + template + rule)."""

    event: EventCreateSchema
    series_name: OneToOneFiftyString
    series_description: StrippedString | None = None
    recurrence: RecurrenceRuleCreateSchema
    auto_publish: bool = False
    generation_window_weeks: int = Field(default=8, ge=1, le=MAX_GENERATION_WINDOW_WEEKS)


class TemplateEditSchema(Schema):
    """Schema for editing a recurring series template event.

    Deliberately excludes fields that are per-occurrence state (start, end,
    slug), series binding (event_series_id), or that must not be set on a
    template (status — templates stay DRAFT). ``venue_id`` is also omitted
    because venue re-binding belongs on a dedicated endpoint that can enforce
    organization scope, not on the template patch.

    ``rsvp_before`` and ``apply_before`` are intentionally NOT editable here:
    they are per-occurrence offsets anchored to the event's start and are
    shifted at materialization time by the delta between the template start
    and the occurrence start. Changing the offset after creation would require
    recomputing every future occurrence's deadline, which is out of scope for
    Phase 1/2. Edit these per-occurrence via the standard event edit endpoint
    (the occurrence will be marked ``is_modified=True``).
    """

    name: OneToOneFiftyString | None = None
    address_visibility: ResourceVisibility | None = None
    description: StrippedString | None = None
    event_type: Event.EventType | None = None
    visibility: Event.Visibility | None = None
    invitation_message: StrippedString | None = Field(default=None, description="Invitation message")
    max_attendees: int | None = None
    max_tickets_per_user: int | None = Field(default=None, description="Max tickets per user (null = unlimited)")
    waitlist_open: bool | None = None
    requires_full_profile: bool | None = None
    potluck_open: bool | None = None
    accept_invitation_requests: bool | None = None
    public_pronoun_distribution: bool | None = None
    can_attend_without_login: bool | None = None
    requires_ticket: bool | None = None
    address: StrippedString | None = Field(default=None, max_length=255)


class CancelOccurrenceSchema(Schema):
    """Schema for cancelling a single occurrence."""

    occurrence_date: AwareDatetime


class GenerateSeriesEventsSchema(Schema):
    """Schema for manual event generation."""

    until: AwareDatetime | None = None


class EventSeriesRecurrenceUpdateSchema(Schema):
    """Schema for updating series recurrence settings."""

    recurrence: RecurrenceRuleUpdateSchema | None = None
    auto_publish: bool | None = None
    generation_window_weeks: int | None = Field(default=None, ge=1, le=MAX_GENERATION_WINDOW_WEEKS)


class EventSeriesRecurrenceDetailSchema(Schema):
    """Full series detail including recurrence rule and template.

    ``exdates`` is documented as a list of ISO 8601 datetime strings (this is
    how the model stores them in JSON). Clients should ``Date.parse()`` each
    entry. Switching to ``list[AwareDatetime]`` would require a custom
    field_validator that interferes with Ninja's response-schema generation
    when the schema is re-used inside other controllers' import graphs, so we
    keep the wire type as a list of strings and document the format here.
    """

    id: UUID
    name: str
    slug: str
    description: str | None = None
    is_active: bool
    auto_publish: bool
    generation_window_weeks: int
    exdates: list[str] = Field(
        default_factory=list,
        description="List of cancelled occurrence instants as ISO 8601 datetime strings (UTC).",
    )
    last_generated_until: AwareDatetime | None = None
    recurrence_rule: RecurrenceRuleSchema | None = None
    template_event: MinimalEventSchema | None = None
