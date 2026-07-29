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


def build_visibility_settings_update(stored: dict[str, t.Any] | None, payload: BaseModel) -> dict[str, t.Any]:
    """Build the merged ``visibility_settings`` write for an edit payload, if any.

    Editing endpoints write ``model_dump(exclude_unset=True)``, so omitting a
    field leaves it unchanged — and pydantic propagates ``exclude_unset`` into
    nested models, so the payload carries exactly the toggles the client named.
    Writing that verbatim would *replace* the stored blob and silently re-enable
    every toggle the client left out: an organizer who had hidden the attendee
    count would have it disclosed again by an unrelated edit to
    ``show_capacity``. That is precisely the failure this feature exists to
    prevent, so the sent toggles are merged onto the stored ones instead.

    Merging gives the nested object the same "omit means unchanged" contract
    every sibling field on the edit schemas already has, just at sub-key
    granularity. Naming a toggle explicitly still sets it, so re-enabling a
    disclosure stays possible — it only has to be deliberate.

    Args:
        stored: The event's current ``visibility_settings`` (possibly empty/None).
        payload: The validated edit payload, which may or may not carry the field.

    Returns:
        ``{"visibility_settings": <merged>}``, or an empty mapping when there is
        nothing to write, so callers can splat it unconditionally.

    Note:
        The two edit schemas differ on explicit ``null`` and both are handled
        here. ``EventEditSchema`` declares the field non-nullable, so a ``null``
        never reaches this function — pydantic rejects it with a 422.
        ``TemplateEditSchema`` declares it ``| None = None`` like every one of
        its siblings, so a ``null`` does arrive; it is treated as "no change"
        rather than written through, which would violate the column's NOT NULL.
    """
    sent = payload.model_dump(exclude_unset=True).get("visibility_settings")
    if not isinstance(sent, dict):
        return {}
    return {"visibility_settings": {**(stored or {}), **sent}}
