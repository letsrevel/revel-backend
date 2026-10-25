"""Notification dates are rendered in the recipient's language (#1214)."""

import typing as t
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.gis.geos import Point
from django.utils import translation

from events.models import Event, Organization
from events.utils import format_event_datetime
from geo.models import City
from notifications.utils import format_datetime, get_formatted_context_for_template

pytestmark = pytest.mark.django_db

# 2026-10-26 is a Monday; Vienna is on CET (+1) after DST ends on 25 Oct.
START = datetime(2026, 10, 26, 12, 20, tzinfo=UTC)
EN_START = "Monday, October 26, 2026 at 1:20 PM CET"
DE_START = "Montag, 26. Oktober 2026, 13:20 CET"


@pytest.fixture
def vienna_event(organization: Organization) -> Event:
    city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=99999,
        location=Point(16.3738, 48.2082, srid=4326),
        timezone="Europe/Vienna",
    )
    return Event.objects.create(
        organization=organization,
        name="Vienna Party",
        slug="vienna-party",
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=START,
        end=START + timedelta(hours=3),
        city=city,
    )


def _ctx(event: Event, **extra: t.Any) -> dict[str, t.Any]:
    return {"event_id": str(event.id), "event_name": event.name, **extra}


def test_format_event_datetime_english_and_german(vienna_event: Event) -> None:
    assert format_event_datetime(START, vienna_event) == EN_START
    with translation.override("de"):
        assert format_event_datetime(START, vienna_event) == DE_START


def test_format_datetime_localised() -> None:
    with translation.override("en"):
        assert format_datetime(START.isoformat(), "full") == "Monday, October 26, 2026 at 12:20 PM UTC"
        assert format_datetime(START.isoformat(), "short") == "Oct 26, 2026 at 12:20 PM"
    with translation.override("de"):
        assert format_datetime(START.isoformat(), "full") == "Montag, 26. Oktober 2026, 12:20 UTC"
        assert format_datetime(START.isoformat(), "short") == "26. Okt 2026, 12:20"


def test_enrichment_overrides_english_prebaked_dates(vienna_event: Event) -> None:
    ctx = _ctx(
        vienna_event,
        event_start_formatted=EN_START,
        event_end_formatted="Monday, October 26, 2026 at 4:20 PM CET",
    )

    enriched = get_formatted_context_for_template(ctx, user_language="de")

    assert enriched["event_start_formatted"] == DE_START
    assert enriched["event_end_formatted"] == "Montag, 26. Oktober 2026, 16:20 CET"
    assert ctx["event_start_formatted"] == EN_START  # input untouched


def test_enrichment_does_not_add_absent_keys(vienna_event: Event) -> None:
    enriched = get_formatted_context_for_template(
        _ctx(vienna_event, event_start_formatted=EN_START), user_language="de"
    )

    assert "event_end_formatted" not in enriched


def test_enrichment_localises_waitlist_expiry(vienna_event: Event) -> None:
    expires = datetime.now(UTC) + timedelta(hours=2, minutes=5)
    ctx = _ctx(
        vienna_event,
        expires_at=expires.isoformat(),
        expires_at_formatted="Monday, October 26, 2026 at 1:20 PM CET",
        time_remaining_formatted="2 hours from now",
    )

    enriched = get_formatted_context_for_template(ctx, user_language="de")

    with translation.override("de"):
        assert enriched["expires_at_formatted"] == format_event_datetime(expires, vienna_event)
    assert enriched["expires_at_formatted"] != ctx["expires_at_formatted"]
    assert "Stunde" in enriched["time_remaining_formatted"]


def test_enrichment_leaves_values_when_event_is_gone(vienna_event: Event) -> None:
    ctx = _ctx(vienna_event, event_start_formatted=EN_START)
    vienna_event.delete()

    enriched = get_formatted_context_for_template(ctx, user_language="de")

    assert enriched["event_start_formatted"] == EN_START
