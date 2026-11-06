"""Schemas for private organizer notes (staff-only)."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field

# Generous payload bound only; the real cap (on unescaped text) is enforced in the service.
USER_NOTE_PAYLOAD_MAX_LENGTH = 30000


class UserNoteSchema(Schema):
    """An organization's private note about a user."""

    body: str
    updated_at: AwareDatetime
    updated_by_id: UUID | None = None


class UserNoteUpsertSchema(Schema):
    """Payload to create or replace the private note about a user."""

    body: str = Field(..., min_length=1, max_length=USER_NOTE_PAYLOAD_MAX_LENGTH)
