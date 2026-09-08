"""Sanitisation of the ``attribution`` checkout field (#922).

Values are trusted-nothing: the dict comes off the buyer's URL. Anything that does
not look like a campaign tag is dropped rather than rejected — attribution is
best-effort metadata and must never block a purchase.
"""

import pytest
from pydantic import ValidationError

from events.models import TicketAttribution
from events.schema.attribution import AttributionPayloadMixin


class _Payload(AttributionPayloadMixin):
    """Bare carrier so the mixin can be exercised without a checkout cart."""


def _parse(raw: object) -> TicketAttribution | None:
    """Validate ``{"attribution": raw}`` exactly as an incoming request body would be."""
    return _Payload.model_validate({"attribution": raw}).attribution


def test_absent_attribution_is_none() -> None:
    assert _Payload.model_validate({}).attribution is None


def test_explicit_null_is_none() -> None:
    assert _parse(None) is None


def test_keeps_the_four_utm_keys_and_strips_whitespace() -> None:
    assert _parse(
        {
            "utm_source": " newsletter ",
            "utm_medium": "email",
            "utm_campaign": "spring-2026",
            "utm_content": "example.org",
        }
    ) == {
        "utm_source": "newsletter",
        "utm_medium": "email",
        "utm_campaign": "spring-2026",
        "utm_content": "example.org",
    }


def test_unknown_keys_are_dropped() -> None:
    assert _parse({"utm_source": "x", "utm_term": "shoes", "referrer": "evil"}) == {"utm_source": "x"}


def test_values_are_capped_at_100_chars() -> None:
    assert _parse({"utm_campaign": "a" * 150}) == {"utm_campaign": "a" * 100}


@pytest.mark.parametrize(
    "bad",
    ["has space", "semi;colon", "<script>", "quote'd", "slash/es", "ünïcode", ""],
)
def test_values_with_disallowed_characters_are_dropped(bad: str) -> None:
    assert _parse({"utm_source": bad, "utm_medium": "ok_1.2:3-4"}) == {"utm_medium": "ok_1.2:3-4"}


def test_non_string_values_are_dropped() -> None:
    assert _parse({"utm_source": 42, "utm_medium": None, "utm_campaign": "ok"}) == {"utm_campaign": "ok"}


def test_empty_after_sanitisation_becomes_none() -> None:
    assert _parse({}) is None
    assert _parse({"utm_term": "x"}) is None
    assert _parse({"utm_source": "bad value"}) is None


def test_non_object_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parse("utm_source=x")
    with pytest.raises(ValidationError):
        _parse(["utm_source", "x"])
