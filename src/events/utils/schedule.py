"""Pydantic schemas + helpers for an event's display-only schedule/timeline.

This module is pure utility — no DB access, no imports from services.
Safe to import from models, admin, schemas, and service code alike.
Mirrors ``events/utils/refund_policy.py``.
"""

import typing as t

from annotated_types import Len
from pydantic import BaseModel, Field, TypeAdapter, field_validator

from common.fields import sanitize_markdown


class EventScheduleSession(BaseModel):
    """One entry on an event's timeline. Display-only; never referenced by ID.

    Times are stored as a relative ``offset_minutes`` from the event start, so the
    schedule survives event duplication/recurrence verbatim with no date-shifting.
    """

    title: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)  # rendered as markdown by FE
    offset_minutes: int = Field(ge=0)  # minutes after event.start
    duration_minutes: int | None = Field(default=None, gt=0)  # None = open-ended
    location: str | None = Field(default=None, max_length=150)  # free text, e.g. "Main Hall"
    is_required: bool = False  # display badge only

    @field_validator("description")
    @classmethod
    def _bleach_description(cls, v: str | None) -> str | None:
        """Sanitize markdown-rendered content (nh3).

        JSONField storage bypasses ``MarkdownField``'s pre_save sanitization, so we
        bleach the only frontend-markdown-rendered field here instead. ``title`` and
        ``location`` are plain text (frontend auto-escapes) and are left untouched.
        """
        return sanitize_markdown(v) if v is not None else None


ScheduleSessions = t.Annotated[list[EventScheduleSession], Len(max_length=200)]
_SCHEDULE_ADAPTER: TypeAdapter[list[EventScheduleSession]] = TypeAdapter(ScheduleSessions)


def validate_schedule(data: list[t.Any] | None) -> list[EventScheduleSession]:
    """Parse & validate a stored/inbound schedule.

    Args:
        data: Raw list (e.g. from JSONField) or None.

    Returns:
        List of validated ``EventScheduleSession`` (empty when ``data`` is None).

    Raises:
        pydantic.ValidationError: if ``data`` is malformed or exceeds 200 sessions.
    """
    return _SCHEDULE_ADAPTER.validate_python(data if data is not None else [])
