"""Drift guard: the Apple and Google rails must describe the same pass.

Both builders' docstrings claim their venue/sector/seat/price and series-window
resolution mirrors the other rail's, but nothing enforced it — the two code
paths were textually duplicated. They now share :mod:`wallet.resolution`; this
test pins the claim so a future change to one rail cannot silently diverge from
the other (same idea as ``polls/tests/test_controller_drift.py``).
"""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from events.models import Event, HeldSeriesPass, Ticket
from events.utils import get_event_timezone
from wallet.apple.formatting import format_iso_date
from wallet.apple.generator import ApplePassGenerator
from wallet.google.builder import build_series_pass_payload, build_ticket_payload
from wallet.tests.conftest import make_covered_event

pytestmark = pytest.mark.django_db


def _google_venue(cls: dict[str, t.Any]) -> tuple[str | None, str | None]:
    """Unwrap Google's EventVenue into ``(name, address)``."""
    venue = cls.get("venue")
    if venue is None:
        return None, None
    return venue["name"]["defaultValue"]["value"], venue["address"]["defaultValue"]["value"]


def _google_seat_info(obj: dict[str, t.Any]) -> tuple[str | None, str | None]:
    """Unwrap Google's seatInfo into ``(section, seat)``."""
    seat_info = obj.get("seatInfo", {})
    section = seat_info.get("section")
    seat = seat_info.get("seat")
    return (
        section["defaultValue"]["value"] if section else None,
        seat["defaultValue"]["value"] if seat else None,
    )


def _google_price(obj: dict[str, t.Any]) -> str:
    """Read the price text module off a Google object."""
    return str(next(module["body"] for module in obj["textModulesData"] if module["id"] == "price"))


class TestTicketParity:
    """Both rails report the same venue, sector, seat and price for one ticket."""

    def test_seated_ticket_matches_across_rails(
        self,
        google_wallet_configured_settings: None,
        seated_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Tier venue/sector + assigned seat resolve identically on both rails."""
        apple = ApplePassGenerator(signer=mock_signer)._build_pass_data(seated_ticket)
        payload = build_ticket_payload(seated_ticket)
        cls, obj = payload["eventTicketClasses"][0], payload["eventTicketObjects"][0]

        google_venue_name, google_address = _google_venue(cls)
        google_section, google_seat = _google_seat_info(obj)

        assert apple.venue_name == google_venue_name == "Teatro Grande"
        assert apple.address == google_address == "1 Teatro Street"
        assert apple.sector_name == google_section == "Platea"
        assert apple.seat_label == google_seat == "A-7"
        assert apple.ticket_price == _google_price(obj) == "EUR 10.00"

    def test_ticket_venue_falls_back_to_the_event_address_on_both_rails(
        self,
        google_wallet_configured_settings: None,
        ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """No venue anywhere: both rails fall back to ``event.address``, and report no seat."""
        apple = ApplePassGenerator(signer=mock_signer)._build_pass_data(ticket)
        payload = build_ticket_payload(ticket)
        cls, obj = payload["eventTicketClasses"][0], payload["eventTicketObjects"][0]

        _, google_address = _google_venue(cls)
        google_section, google_seat = _google_seat_info(obj)

        assert apple.venue_name is None
        assert apple.address == google_address == ticket.event.address
        assert apple.sector_name == google_section is None
        assert apple.seat_label == google_seat is None

    def test_free_ticket_price_matches_across_rails(
        self,
        google_wallet_configured_settings: None,
        ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """A zero-price ticket reads "Free" on both rails (no rail-local special case)."""
        ticket.tier.price = 0
        ticket.tier.save()

        apple = ApplePassGenerator(signer=mock_signer)._build_pass_data(ticket)
        obj = build_ticket_payload(ticket)["eventTicketObjects"][0]

        assert apple.ticket_price == _google_price(obj) == "Free"


class TestSeriesPassParity:
    """Both rails derive the same representative event and validity window."""

    def test_series_window_matches_across_rails(
        self,
        google_wallet_configured_settings: None,
        held_pass: HeldSeriesPass,
        covered_events: list[Event],
        mock_signer: MagicMock,
    ) -> None:
        """The soonest upcoming covered event drives start; the latest end drives expiry."""
        representative = covered_events[0]
        representative.venue = None
        representative.address = "9 Series Street"
        representative.save()

        apple = ApplePassGenerator(signer=mock_signer)._build_series_pass_data(held_pass)
        payload = build_series_pass_payload(held_pass)
        cls, obj = payload["eventTicketClasses"][0], payload["eventTicketObjects"][0]

        _, google_address = _google_venue(cls)
        tz = get_event_timezone(representative)

        assert apple.address == google_address == "9 Series Street"
        assert cls["dateTime"]["start"] == format_iso_date(apple.event_start, tz=tz)
        assert cls["dateTime"]["end"] == format_iso_date(apple.event_end, tz=tz)
        assert apple.event_start == representative.start
        assert apple.event_end == max(event.end for event in covered_events)
        assert apple.ticket_price == _google_price(obj) == "EUR 50.00"

    def test_past_series_representative_matches_across_rails(
        self,
        google_wallet_configured_settings: None,
        organization: t.Any,
        event_series: t.Any,
        series_pass: t.Any,
        member_user: t.Any,
        mock_signer: MagicMock,
    ) -> None:
        """Once every covered event has ended, both rails pick the latest-starting past one."""
        make_covered_event(organization, event_series, series_pass, "Old One", "old-one", timedelta(days=-14))
        latest = make_covered_event(organization, event_series, series_pass, "Old Two", "old-two", timedelta(days=-7))
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=0,
        )

        apple = ApplePassGenerator(signer=mock_signer)._build_series_pass_data(held)
        cls = build_series_pass_payload(held)["eventTicketClasses"][0]

        assert apple.event_start == latest.start
        assert cls["dateTime"]["start"] == format_iso_date(apple.event_start, tz=get_event_timezone(latest))

    def test_series_pass_without_covered_events_matches_across_rails(
        self,
        google_wallet_configured_settings: None,
        series_pass: t.Any,
        member_user: t.Any,
        mock_signer: MagicMock,
    ) -> None:
        """Defensive: with no covered events both rails fall back to ``created_at`` and no venue."""
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=0,
        )

        apple = ApplePassGenerator(signer=mock_signer)._build_series_pass_data(held)
        cls = build_series_pass_payload(held)["eventTicketClasses"][0]

        assert apple.event_start == apple.event_end == held.created_at
        assert apple.venue_name is None
        assert apple.address is None
        assert "venue" not in cls
