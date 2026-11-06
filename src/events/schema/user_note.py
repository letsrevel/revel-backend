"""Schemas for private organizer notes (staff-only)."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field

from events.models.user_note import USER_NOTE_MAX_LENGTH


class UserNoteSchema(Schema):
    """An organization's private note about a user."""

    body: str
    updated_at: AwareDatetime
    updated_by_id: UUID | None = None


class UserNoteUpsertSchema(Schema):
    """Payload to create or replace the private note about a user."""

    body: str = Field(..., min_length=1, max_length=USER_NOTE_MAX_LENGTH)
