"""Purchase attribution payload field (#922).

One mixin shared by every checkout payload (authenticated, guest, series pass), so the
sanitiser has a single authority. The guest confirmation JWT carries the already-sanitised
value as a plain field and never re-validates it.
"""

import re
import typing as t

from ninja import Schema
from pydantic import Field, field_validator

from events.models import TicketAttribution

_ALLOWED_KEYS: t.Final = ("utm_source", "utm_medium", "utm_campaign", "utm_content")
_MAX_LEN: t.Final = 100
# Mirrors the FE ``sanitizeUtmContent`` (src/lib/embed/params.ts): tag-shaped
# characters only. Values land in exports and admin tables, never in HTML, but
# the loader script forwards whatever a third-party host page carried.
_ALLOWED_VALUE: t.Final = re.compile(r"^[A-Za-z0-9._:\-]+$")


def sanitize_attribution(raw: t.Mapping[str, object]) -> TicketAttribution | None:
    """Keep only well-formed ``utm_*`` tags; ``None`` when nothing survives.

    Best-effort by design: a malformed value is dropped, never a reason to 400 a
    checkout. Truncation happens before the character check so an over-long but
    otherwise valid tag keeps its first 100 characters, exactly like the FE.

    Args:
        raw: The dict as sent by the client.

    Returns:
        The surviving tags, or ``None`` when the dict is empty after cleaning.
    """
    cleaned: TicketAttribution = {}
    for key in _ALLOWED_KEYS:
        value = raw.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()[:_MAX_LEN]
        if _ALLOWED_VALUE.match(value):
            cleaned[key] = value
    return cleaned or None


class AttributionPayloadMixin(Schema):
    """Adds the optional ``attribution`` field to a checkout payload."""

    attribution: TicketAttribution | None = Field(
        default=None,
        description="Campaign tags (utm_source/medium/campaign/content) read off the page URL at "
        "checkout. Malformed values are dropped silently; omit when the URL carried none.",
    )

    @field_validator("attribution", mode="before")
    @classmethod
    def _sanitize(cls, v: object) -> object:
        """Drop unknown keys and malformed values before the TypedDict is validated."""
        if isinstance(v, t.Mapping):
            return sanitize_attribution(v)
        return v
