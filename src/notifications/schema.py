"""Schemas for notification API."""

from datetime import datetime
from typing import Any
from uuid import UUID

from ninja import ModelSchema, Schema

from notifications.models import NotificationPreference


class NotificationSchema(Schema):
    """Schema for notification response."""

    id: UUID
    notification_type: str
    title: str
    body: str
    context: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class UnreadCountSchema(Schema):
    """Schema for unread count response."""

    count: int


class MarkReadResponseSchema(Schema):
    """Schema for mark read/unread responses."""

    success: bool


class NotificationPreferenceSchema(ModelSchema):
    """Schema for notification preferences."""

    # Only declare fields that need special handling (enum/type conversion)
    digest_frequency: str
    digest_send_time: str
    show_me_on_attendee_list: str

    class Meta:
        model = NotificationPreference
        fields = [
            "silence_all_notifications",
            "enabled_channels",
            "digest_frequency",
            "digest_send_time",
            "event_reminders_enabled",
            "notification_type_settings",
            "show_me_on_attendee_list",
        ]


class UpdateNotificationPreferenceSchema(Schema):
    """Schema for updating notification preferences."""

    silence_all_notifications: bool | None = None
    enabled_channels: list[str] | None = None
    digest_frequency: str | None = None
    digest_send_time: str | None = None
    event_reminders_enabled: bool | None = None
