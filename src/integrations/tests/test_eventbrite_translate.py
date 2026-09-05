"""Neutral ⇄ Eventbrite JSON on recorded shapes."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.providers.base import RemoteEvent, RemoteTicketClass, RemoteVenue
from integrations.providers.eventbrite import translate as tr

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text())  # type: ignore[no-any-return]


START = datetime(2026, 10, 4, 11, 4, 17, tzinfo=UTC)


def test_event_payload_shape_and_no_description() -> None:
    ev = RemoteEvent(
        name="Spike",
        summary="Short.",
        description_html="<p>long</p>",
        start=START,
        end=START + timedelta(hours=3),
        timezone="Europe/Vienna",
        currency="EUR",
    )
    body = tr.to_eventbrite_event(ev, venue_id="299287124")["event"]
    assert body["name"] == {"html": "Spike"} and body["summary"] == "Short."
    assert body["start"] == {"timezone": "Europe/Vienna", "utc": "2026-10-04T11:04:17Z"}
    assert body["end"]["utc"] == "2026-10-04T14:04:17Z"
    assert (
        body["currency"] == "EUR"
        and body["listed"] is False
        and body["online_event"] is False
        and body["venue_id"] == "299287124"
    )
    assert "description" not in body


def test_event_payload_omits_empty_summary_and_venue() -> None:
    ev = RemoteEvent(
        name="Online", start=START, end=START + timedelta(hours=1), timezone="UTC", currency="EUR", is_virtual=True
    )
    body = tr.to_eventbrite_event(ev, venue_id=None)["event"]
    assert "summary" not in body and "venue_id" not in body and body["online_event"] is True


def test_ticket_class_paid_and_free() -> None:
    paid = RemoteTicketClass(
        name="Regular",
        price=Decimal("15.00"),
        currency="EUR",
        is_free=False,
        quantity_total=100,
        sales_start=START,
        hidden=True,
    )
    body = tr.to_eventbrite_ticket_class(paid)["ticket_class"]
    assert body == {
        "name": "Regular",
        "quantity_total": 100,
        "hidden": True,
        "cost": "EUR,1500",
        "sales_start": "2026-10-04T11:04:17Z",
    }
    free = RemoteTicketClass(
        name="Free", price=Decimal("0"), currency="EUR", is_free=True, quantity_total=50, description="Bring ID"
    )
    body = tr.to_eventbrite_ticket_class(free)["ticket_class"]
    assert body == {"name": "Free", "quantity_total": 50, "hidden": False, "free": True, "description": "Bring ID"}


@pytest.mark.parametrize(("price", "minor"), [("15.00", 1500), ("0.1", 10), ("19.995", 2000), ("7", 700)])
def test_minor_units(price: str, minor: int) -> None:
    assert tr.minor_units(Decimal(price)) == minor


def test_venue_payload() -> None:
    v = RemoteVenue(
        name="Hall",
        address="Stephansplatz 1",
        city="Wien",
        postal_code="1010",
        country="AT",
        latitude=48.2,
        longitude=16.37,
    )
    assert tr.to_eventbrite_venue(v) == {
        "venue": {
            "name": "Hall",
            "address": {"address_1": "Stephansplatz 1", "city": "Wien", "postal_code": "1010", "country": "AT"},
            "latitude": "48.2",
            "longitude": "16.37",
        }
    }


def test_structured_content_payload() -> None:
    assert tr.to_eventbrite_structured_content("<p>x</p>") == {
        "modules": [{"type": "text", "data": {"body": {"text": "<p>x</p>", "alignment": "left"}}}],
        "publish": True,
    }


def test_from_event_expanded_fixture() -> None:
    ev = tr.from_eventbrite_event(_load("event_get_expanded"))
    assert ev.remote_id == "1999760883635" and ev.name == "Spike Event (throwaway)"
    assert ev.start == START and ev.timezone == "Europe/Vienna" and ev.currency == "EUR" and ev.status == "draft"
    assert (
        ev.venue is not None
        and ev.venue.city == "Wien"
        and ev.venue.country == "AT"
        and ev.venue.latitude == pytest.approx(48.2084609)
    )
    assert ev.url.startswith("https://www.eventbrite.com/e/")


def test_from_ticket_class_fixture() -> None:
    tc = tr.from_eventbrite_ticket_class(_load("ticket_class_create_paid"))
    assert (
        tc.remote_id == "3451654576" and tc.name == "Regular" and tc.price == Decimal("15.00") and tc.currency == "EUR"
    )
    assert tc.is_free is False and tc.quantity_total == 100 and tc.quantity_sold == 0 and tc.hidden is False
    assert tc.sales_start is not None and tc.sales_start.tzinfo is not None


def test_from_summary_and_status_mapping() -> None:
    data = _load("events_list")["events"][0]  # type: ignore[index]
    s = tr.from_eventbrite_summary(data)
    assert s.remote_id == "1999760883635" and s.status == "draft"
    assert tr.status_from_eventbrite("live") == "live" and tr.status_from_eventbrite("started") == "live"
    assert tr.status_from_eventbrite("canceled") == "cancelled" and tr.status_from_eventbrite("draft") == "draft"
