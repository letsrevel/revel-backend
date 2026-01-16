"""Follow-related schemas."""

import typing as t
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime

from events.models.follow import EventSeriesFollow, OrganizationFollow

from .event_series import MinimalEventSeriesSchema
from .organization import MinimalOrganizationSchema


class OrganizationFollowSchema(Schema):
    """Schema for organization follow with full organization details."""

    id: UUID
    organization: MinimalOrganizationSchema
    notify_new_events: bool
    notify_announcements: bool
    is_public: bool
    created_at: AwareDatetime

    @classmethod
    def from_model(cls, follow: OrganizationFollow) -> t.Self:
        """Create schema from OrganizationFollow model instance.

        Args:
            follow: OrganizationFollow instance with organization prefetched.

        Returns:
            OrganizationFollowSchema instance.
        """
        return cls(
            id=follow.id,
            organization=MinimalOrganizationSchema.model_validate(follow.organization, from_attributes=True),
            notify_new_events=follow.notify_new_events,
            notify_announcements=follow.notify_announcements,
            is_public=follow.is_public,
            created_at=follow.created_at,
        )


class EventSeriesFollowSchema(Schema):
    """Schema for event series follow with full series details."""

    id: UUID
    event_series: MinimalEventSeriesSchema
    notify_new_events: bool
    is_public: bool
    created_at: AwareDatetime

    @classmethod
    def from_model(cls, follow: EventSeriesFollow) -> t.Self:
        """Create schema from EventSeriesFollow model instance.

        Args:
            follow: EventSeriesFollow instance with event_series prefetched.

        Returns:
            EventSeriesFollowSchema instance.
        """
        return cls(
            id=follow.id,
            event_series=MinimalEventSeriesSchema.model_validate(follow.event_series, from_attributes=True),
            notify_new_events=follow.notify_new_events,
            is_public=follow.is_public,
            created_at=follow.created_at,
        )


class OrganizationFollowCreateSchema(Schema):
    """Schema for creating an organization follow."""

    notify_new_events: bool = True
    notify_announcements: bool = True


class OrganizationFollowUpdateSchema(Schema):
    """Schema for updating an organization follow's notification preferences."""

    notify_new_events: bool | None = None
    notify_announcements: bool | None = None


class EventSeriesFollowCreateSchema(Schema):
    """Schema for creating an event series follow."""

    notify_new_events: bool = True


class EventSeriesFollowUpdateSchema(Schema):
    """Schema for updating an event series follow's notification preferences."""

    notify_new_events: bool | None = None


class FollowStatusSchema(Schema):
    """Schema for checking follow status."""

    is_following: bool
    follow: OrganizationFollowSchema | EventSeriesFollowSchema | None = None


class OrganizationFollowStatusSchema(Schema):
    """Schema for organization follow status."""

    is_following: bool
    follow: OrganizationFollowSchema | None = None


class EventSeriesFollowStatusSchema(Schema):
    """Schema for event series follow status."""

    is_following: bool
    follow: EventSeriesFollowSchema | None = None


class MinimalOrganizationFollowSchema(ModelSchema):
    """Lightweight follow schema without nested organization."""

    class Meta:
        model = OrganizationFollow
        fields = ["id", "notify_new_events", "notify_announcements", "is_public", "created_at"]


class MinimalEventSeriesFollowSchema(ModelSchema):
    """Lightweight follow schema without nested event series."""

    class Meta:
        model = EventSeriesFollow
        fields = ["id", "notify_new_events", "is_public", "created_at"]
