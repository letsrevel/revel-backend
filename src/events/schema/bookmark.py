"""Schemas for event bookmarks."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime


class EventBookmarkSchema(Schema):
    """A user's bookmark for an event."""

    id: UUID
    event_id: UUID
    created_at: AwareDatetime
