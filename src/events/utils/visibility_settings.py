"""Pydantic schema + helper for an event's granular visibility settings.

This module is pure utility — no DB access, no imports from services.
Safe to import from models, admin, schemas, and service code alike.
Mirrors ``events/utils/schedule.py`` and ``events/utils/refund_policy.py``.
"""

import typing as t

from pydantic import BaseModel, ConfigDict, TypeAdapter


class EventVisibilitySettings(BaseModel):
    """Per-event switches controlling disclosure of organizational information.

    Every toggle defaults to ``True``, which reproduces the pre-#792 behavior:
    counts, capacity and the guest list were always disclosed. Organization
    owners and staff bypass all of these — they always see the real numbers.

    Note:
        ``show_attendee_list`` is ANDed with each attendee's own
        ``show_me_on_attendee_list`` preference: turning it off hides the guest
        list even for users who opted in, but turning it on never overrides a
        user who opted out.
    """

    model_config = ConfigDict(extra="forbid")

    show_attendee_count: bool = True
    show_capacity: bool = True
    show_attendee_list: bool = True


_ADAPTER: TypeAdapter[EventVisibilitySettings] = TypeAdapter(EventVisibilitySettings)


def validate_visibility_settings(data: dict[str, t.Any] | None) -> EventVisibilitySettings:
    """Parse & validate a stored/inbound visibility-settings blob.

    Args:
        data: Raw mapping (e.g. from a JSONField) or None.

    Returns:
        A validated ``EventVisibilitySettings`` (all-defaults when ``data`` is
        None or an empty dict — the stored representation for untouched events).

    Raises:
        pydantic.ValidationError: if ``data`` is malformed or carries unknown keys.
    """
    return _ADAPTER.validate_python(data if data is not None else {})
