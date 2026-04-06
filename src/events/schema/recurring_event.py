"""Schemas for recurring event operations."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime

from common.schema import OneToOneFiftyString, StrippedString

from .event import EventCreateSchema, MinimalEventSchema
from .recurrence_rule import RecurrenceRuleCreateSchema, RecurrenceRuleSchema, RecurrenceRuleUpdateSchema


class RecurringEventCreateSchema(Schema):
    """Schema for creating a recurring event (series + template + rule)."""

    event: EventCreateSchema
    series_name: OneToOneFiftyString
    series_description: StrippedString | None = None
    recurrence: RecurrenceRuleCreateSchema
    auto_publish: bool = False
    generation_window_weeks: int = 8


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
    generation_window_weeks: int | None = None


class EventSeriesRecurrenceDetailSchema(Schema):
    """Full series detail including recurrence rule and template."""

    id: UUID
    name: str
    slug: str
    description: str | None = None
    is_active: bool
    auto_publish: bool
    generation_window_weeks: int
    exdates: list[str]
    last_generated_until: AwareDatetime | None = None
    recurrence_rule: RecurrenceRuleSchema | None = None
    template_event: MinimalEventSchema | None = None
