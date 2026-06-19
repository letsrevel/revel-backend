"""Announcement-related schemas."""

import datetime as dt
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, Field, model_validator

from common.schema import OneToOneFiftyString, StrippedString
from events.models import Announcement


class AnnouncementCreateSchema(Schema):
    """Schema for creating a draft announcement."""

    title: OneToOneFiftyString
    body: StrippedString

    # Targeting options (mutually exclusive)
    event_id: UUID4 | None = None
    target_all_members: bool = False
    target_tier_ids: list[UUID4] = Field(default_factory=list)
    target_staff_only: bool = False

    past_visibility: bool = True
    resend_to_new_signups: bool = False

    @model_validator(mode="after")
    def validate_targeting(self) -> "AnnouncementCreateSchema":
        """Ensure exactly one targeting option is selected."""
        options = [
            self.event_id is not None,
            self.target_all_members,
            bool(self.target_tier_ids),
            self.target_staff_only,
        ]
        selected = sum(options)

        if selected == 0:
            raise ValueError(
                "At least one targeting option must be selected: "
                "event_id, target_all_members, target_tier_ids, or target_staff_only"
            )
        if selected > 1:
            raise ValueError(
                "Only one targeting option can be selected at a time: "
                "event_id, target_all_members, target_tier_ids, or target_staff_only"
            )

        if self.resend_to_new_signups:
            if self.event_id is None:
                raise ValueError("resend_to_new_signups requires an event-targeted announcement")
            self.past_visibility = True

        return self


class AnnouncementUpdateSchema(Schema):
    """Schema for updating a draft announcement."""

    title: OneToOneFiftyString | None = None
    body: StrippedString | None = None

    # Targeting options (mutually exclusive)
    event_id: UUID4 | None = None
    target_all_members: bool | None = None
    target_tier_ids: list[UUID4] | None = None
    target_staff_only: bool | None = None

    past_visibility: bool | None = None
    resend_to_new_signups: bool | None = None

    @model_validator(mode="after")
    def validate_targeting(self) -> "AnnouncementUpdateSchema":
        """If any targeting option is provided, ensure only one is set.

        When updating targeting, the user must provide exactly one enabled option
        to prevent leaving the announcement in an invalid state with no targeting.
        """
        if self.resend_to_new_signups:
            self.past_visibility = True

        # Check if any targeting field is being updated (explicitly set, not None)
        targeting_updates = [
            self.event_id,
            self.target_all_members,
            self.target_tier_ids,
            self.target_staff_only,
        ]

        # If no targeting fields are provided, skip validation
        if all(opt is None for opt in targeting_updates):
            return self

        # Count how many targeting options are "truthy" (set to a value that enables them)
        options = [
            self.event_id is not None,
            self.target_all_members is True,
            bool(self.target_tier_ids),
            self.target_staff_only is True,
        ]
        selected = sum(options)

        if selected == 0:
            raise ValueError(
                "When updating targeting, at least one option must be enabled: "
                "event_id, target_all_members, target_tier_ids, or target_staff_only"
            )

        if selected > 1:
            raise ValueError(
                "Only one targeting option can be selected at a time: "
                "event_id, target_all_members, target_tier_ids, or target_staff_only"
            )

        return self


class AnnouncementScheduleSchema(Schema):
    """Schema for scheduling a draft announcement (absolute or relative)."""

    scheduled_at: AwareDatetime | None = None
    schedule_anchor: Announcement.ScheduleAnchor | None = None
    schedule_offset_minutes: int | None = None

    @model_validator(mode="after")
    def validate_schedule(self) -> "AnnouncementScheduleSchema":
        """Require exactly one of: absolute time, or anchor + offset."""
        is_relative = self.schedule_anchor is not None or self.schedule_offset_minutes is not None
        if is_relative:
            if self.scheduled_at is not None:
                raise ValueError("Provide either an absolute time or a relative schedule, not both")
            if self.schedule_anchor is None or self.schedule_offset_minutes is None:
                raise ValueError("Relative scheduling requires both an anchor and an offset")
        elif self.scheduled_at is None:
            raise ValueError("Provide either scheduled_at or schedule_anchor + schedule_offset_minutes")
        return self


class MembershipTierMinimalSchema(Schema):
    """Minimal schema for membership tiers in announcement responses."""

    id: UUID
    name: str


class AnnouncementSchema(ModelSchema):
    """Full schema for announcement details."""

    # Fields requiring special handling (enum, resolvers, nested schema)
    status: Announcement.AnnouncementStatus
    schedule_anchor: Announcement.ScheduleAnchor | None = None
    effective_send_at: AwareDatetime | None = None
    event_id: UUID | None = None
    event_name: str | None = None
    target_tiers: list[MembershipTierMinimalSchema] = Field(default_factory=list)
    created_by_name: str | None = None

    class Meta:
        model = Announcement
        fields = [
            "id",
            "title",
            "body",
            "status",
            "target_all_members",
            "target_staff_only",
            "past_visibility",
            "sent_at",
            "recipient_count",
            "scheduled_at",
            "schedule_anchor",
            "schedule_offset_minutes",
            "resend_to_new_signups",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def resolve_event_id(obj: Announcement) -> UUID | None:
        """Resolve event ID."""
        return obj.event_id

    @staticmethod
    def resolve_effective_send_at(obj: Announcement) -> "dt.datetime | None":
        """Resolve the live effective send time (absolute or relative)."""
        return obj.effective_send_at

    @staticmethod
    def resolve_event_name(obj: Announcement) -> str | None:
        """Resolve event name from prefetched event."""
        if obj.event:
            return obj.event.name
        return None

    @staticmethod
    def resolve_target_tiers(obj: Announcement) -> list[dict[str, str | UUID]]:
        """Resolve target tiers from prefetched data."""
        return [{"id": tier.id, "name": tier.name} for tier in obj.target_tiers.all()]

    @staticmethod
    def resolve_created_by_name(obj: Announcement) -> str | None:
        """Resolve creator name from prefetched user."""
        if obj.created_by:
            return obj.created_by.display_name
        return None


class AnnouncementListSchema(ModelSchema):
    """Lightweight schema for announcement lists."""

    # Fields requiring special handling (enum, resolvers)
    status: Announcement.AnnouncementStatus
    event_id: UUID | None = None
    event_name: str | None = None

    class Meta:
        model = Announcement
        fields = [
            "id",
            "title",
            "status",
            "target_all_members",
            "target_staff_only",
            "sent_at",
            "recipient_count",
            "scheduled_at",
            "resend_to_new_signups",
            "created_at",
        ]

    @staticmethod
    def resolve_event_id(obj: Announcement) -> UUID | None:
        """Resolve event ID."""
        return obj.event_id

    @staticmethod
    def resolve_event_name(obj: Announcement) -> str | None:
        """Resolve event name from prefetched event."""
        if obj.event:
            return obj.event.name
        return None


class AnnouncementPublicSchema(Schema):
    """Schema for public announcement view (attendees/members)."""

    id: UUID
    title: str
    body: str
    audience: Announcement.Audience
    sent_at: AwareDatetime | None = None
    organization_name: str | None = None
    event_name: str | None = None

    @staticmethod
    def resolve_organization_name(obj: Announcement) -> str | None:
        """Resolve organization name from prefetched data."""
        if obj.organization:
            return obj.organization.name
        return None

    @staticmethod
    def resolve_event_name(obj: Announcement) -> str | None:
        """Resolve event name from prefetched event."""
        if obj.event:
            return obj.event.name
        return None


class RecipientCountSchema(Schema):
    """Schema for recipient count preview response."""

    count: int
