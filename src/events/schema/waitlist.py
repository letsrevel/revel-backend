"""Schemas for the advanced waitlist admin surface."""

import datetime

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime

from events.models import WaitlistOffer


class WaitlistSettingsSchema(Schema):
    """Read view of an event's waitlist configuration."""

    waitlist_open: bool
    waitlist_time_window: datetime.timedelta | None
    waitlist_batch_size: int
    waitlist_cutoff_date: AwareDatetime | None
    waitlist_cutoff_window: datetime.timedelta | None
    waitlist_lottery_mode: bool


class WaitlistSettingsUpdateSchema(Schema):
    """Partial update payload for waitlist configuration."""

    waitlist_open: bool | None = None
    waitlist_time_window: datetime.timedelta | None = None
    waitlist_batch_size: int | None = None
    waitlist_cutoff_date: AwareDatetime | None = None
    waitlist_cutoff_window: datetime.timedelta | None = None
    waitlist_lottery_mode: bool | None = None


class WaitlistOfferSchema(ModelSchema):
    """Read view of a waitlist offer (admin)."""

    status: WaitlistOffer.Status

    class Meta:
        model = WaitlistOffer
        fields = [
            "id",
            "user",
            "event",
            "status",
            "expires_at",
            "claimed_at",
            "notified_at",
            "batch_id",
            "is_cutoff_batch",
            "created_at",
        ]
