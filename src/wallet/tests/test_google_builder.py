"""Tests for the Google Wallet payload builder."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from wallet.apple.formatting import get_theme_hex_background
from wallet.google.builder import build_ticket_payload

pytestmark = pytest.mark.django_db


def test_ticket_payload_shape(google_wallet_configured_settings: None, ticket: Ticket) -> None:
    payload = build_ticket_payload(ticket)

    assert len(payload["eventTicketClasses"]) == 1
    assert len(payload["eventTicketObjects"]) == 1
    cls = payload["eventTicketClasses"][0]
    obj = payload["eventTicketObjects"][0]

    event = ticket.event
    assert cls["id"] == f"3388000000012345678.test.event.{event.id}"
    assert cls["issuerName"] == event.organization.name
    assert cls["eventName"]["defaultValue"]["value"] == event.name
    assert cls["reviewStatus"] == "UNDER_REVIEW"
    assert cls["hexBackgroundColor"] == get_theme_hex_background()
    assert cls["dateTime"]["start"].startswith(str(event.start.year))

    assert obj["id"] == f"3388000000012345678.test.ticket.{ticket.id}"
    assert obj["classId"] == cls["id"]
    assert obj["state"] == "ACTIVE"
    assert obj["barcode"] == {"type": "QR_CODE", "value": str(ticket.id)}
    assert obj["ticketType"]["defaultValue"]["value"] == ticket.tier.name
    assert obj["ticketHolderName"] == ticket.guest_name

    # No tier/ticket venue, sector, or seat set, and no org logo / event cover_art: none of
    # the optional fields should appear.
    assert "seatInfo" not in obj
    assert "logo" not in cls
    assert "heroImage" not in cls


def test_ticket_payload_price_module(google_wallet_configured_settings: None, ticket: Ticket) -> None:
    payload = build_ticket_payload(ticket)
    obj = payload["eventTicketObjects"][0]
    price_modules = [m for m in obj["textModulesData"] if m["id"] == "price"]
    assert len(price_modules) == 1
    assert price_modules[0]["body"] == "EUR 10.00"  # tier fixture price


def test_ticket_payload_address_only_event(google_wallet_configured_settings: None, ticket: Ticket) -> None:
    """Event fixture has address='123 Test Street' and no venue: both venue
    sub-fields fall back to the address."""
    payload = build_ticket_payload(ticket)
    cls = payload["eventTicketClasses"][0]
    assert cls["venue"]["name"]["defaultValue"]["value"] == "123 Test Street"
    assert cls["venue"]["address"]["defaultValue"]["value"] == "123 Test Street"


@pytest.fixture
def seated_venue(organization: Organization) -> Venue:
    """A venue with an address, for testing venue/sector/seat resolution."""
    return Venue.objects.create(organization=organization, name="Teatro Grande", address="1 Teatro Street")


@pytest.fixture
def seated_sector(seated_venue: Venue) -> VenueSector:
    """The sector the seated tier sells."""
    return VenueSector.objects.create(venue=seated_venue, name="Platea")


@pytest.fixture
def seated_seat(seated_sector: VenueSector) -> VenueSeat:
    """A materialized seat in the sector."""
    return VenueSeat.objects.create(sector=seated_sector, label="A-7", row_label="A", number=7)


@pytest.fixture
def seated_tier(event: Event, seated_venue: Venue, seated_sector: VenueSector) -> TicketTier:
    """A tier wired to the venue/sector, for user-choice seat assignment."""
    return TicketTier.objects.create(
        event=event,
        name="Platea Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=seated_venue,
        sector=seated_sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
    )


@pytest.fixture
def seated_ticket(event: Event, member_user: RevelUser, seated_tier: TicketTier, seated_seat: VenueSeat) -> Ticket:
    """A ticket for the seated tier, with a materialized seat assigned."""
    return Ticket.objects.create(
        event=event,
        user=member_user,
        tier=seated_tier,
        seat=seated_seat,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )


def test_ticket_payload_venue_sector_seat(google_wallet_configured_settings: None, seated_ticket: Ticket) -> None:
    """Tier venue/sector and the ticket's seat surface on the class and object."""
    payload = build_ticket_payload(seated_ticket)
    cls = payload["eventTicketClasses"][0]
    obj = payload["eventTicketObjects"][0]

    assert cls["venue"]["name"]["defaultValue"]["value"] == "Teatro Grande"
    assert cls["venue"]["address"]["defaultValue"]["value"] == "1 Teatro Street"
    assert obj["seatInfo"]["section"]["defaultValue"]["value"] == "Platea"
    assert obj["seatInfo"]["seat"]["defaultValue"]["value"] == "A-7"


def test_ticket_payload_logo_and_hero_present_when_set(
    google_wallet_configured_settings: None, ticket: Ticket, png_bytes: bytes, settings: t.Any
) -> None:
    """Org logo and event cover_art surface as BASE_URL-prefixed logo/heroImage."""
    organization = ticket.event.organization
    organization.logo.save("logo.png", ContentFile(png_bytes), save=True)
    ticket.event.cover_art.save("cover.png", ContentFile(png_bytes), save=True)

    payload = build_ticket_payload(ticket)
    cls = payload["eventTicketClasses"][0]

    base_url = settings.BASE_URL.rstrip("/")
    assert cls["logo"]["sourceUri"]["uri"] == f"{base_url}{organization.logo.url}"
    assert cls["heroImage"]["sourceUri"]["uri"] == f"{base_url}{ticket.event.cover_art.url}"


def test_ticket_payload_no_holder_name(google_wallet_configured_settings: None, ticket: Ticket) -> None:
    ticket.guest_name = ""
    payload = build_ticket_payload(ticket)
    assert "ticketHolderName" not in payload["eventTicketObjects"][0]


def test_ticket_payload_valid_time_interval(google_wallet_configured_settings: None, ticket: Ticket) -> None:
    from wallet.apple.generator import PASS_EXPIRATION_GRACE_PERIOD

    payload = build_ticket_payload(ticket)
    obj = payload["eventTicketObjects"][0]
    end = obj["validTimeInterval"]["end"]["date"]
    expected_year = str((ticket.event.end + PASS_EXPIRATION_GRACE_PERIOD).year)
    assert end.startswith(expected_year)


# --- Series pass fixtures (minimal setup replicated from
# wallet/tests/test_generator_series_pass.py; not imported since that file's
# fixtures aren't shared via a conftest) ---


@pytest.fixture
def google_event_series(organization: Organization) -> EventSeries:
    """Event series for the Google Wallet series-pass test."""
    return EventSeries.objects.create(organization=organization, name="Google Wallet Series", slug="gwallet-series")


@pytest.fixture
def google_series_pass(google_event_series: EventSeries) -> SeriesPass:
    """Series pass product for the Google Wallet series-pass test."""
    return SeriesPass.objects.create(
        event_series=google_event_series,
        name="Google Wallet Season Pass",
        price=Decimal("60.00"),
        pro_rata_discount=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def _covered_event(
    organization: Organization,
    event_series: EventSeries,
    series_pass: SeriesPass,
    name: str,
    slug: str,
    start_delta: timedelta,
) -> Event:
    """Create an OPEN covered event with a linked tier."""
    now = timezone.now()
    covered = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name=name,
        slug=slug,
        start=now + start_delta,
        end=now + start_delta + timedelta(hours=2),
        requires_ticket=True,
        status=Event.EventStatus.OPEN,
    )
    tier = TicketTier.objects.create(
        event=covered, name=f"{name} Tier", price=10, currency="EUR", payment_method=TicketTier.PaymentMethod.FREE
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=covered, tier=tier)
    return covered


@pytest.fixture
def google_covered_events(
    organization: Organization, google_event_series: EventSeries, google_series_pass: SeriesPass
) -> list[Event]:
    """Two future covered events (7 and 14 days out)."""
    return [
        _covered_event(
            organization, google_event_series, google_series_pass, "GW Covered One", "gw-covered-one", timedelta(days=7)
        ),
        _covered_event(
            organization,
            google_event_series,
            google_series_pass,
            "GW Covered Two",
            "gw-covered-two",
            timedelta(days=14),
        ),
    ]


@pytest.fixture
def held_series_pass(
    google_series_pass: SeriesPass, member_user: RevelUser, google_covered_events: list[Event]
) -> HeldSeriesPass:
    """An active held series pass covering two future events."""
    return HeldSeriesPass.objects.create(
        series_pass=google_series_pass,
        user=member_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("50.00"),
    )


def test_series_pass_payload(
    google_wallet_configured_settings: None, held_series_pass: t.Any, google_covered_events: list[Event]
) -> None:
    from wallet.apple.generator import PASS_EXPIRATION_GRACE_PERIOD
    from wallet.google.builder import build_series_pass_payload

    payload = build_series_pass_payload(held_series_pass)
    cls = payload["eventTicketClasses"][0]
    obj = payload["eventTicketObjects"][0]

    series_pass = held_series_pass.series_pass
    assert cls["id"] == f"3388000000012345678.test.series.{series_pass.id}"
    assert cls["eventName"]["defaultValue"]["value"] == series_pass.name
    assert obj["id"] == f"3388000000012345678.test.pass.{held_series_pass.id}"
    assert obj["barcode"] == {"type": "QR_CODE", "value": held_series_pass.qr_payload}
    assert obj["ticketType"]["defaultValue"]["value"] == "Series Pass"

    # Latest-ending covered event (the 14-days-out one) plus the grace period.
    latest_end = max(event.end for event in google_covered_events)
    end = obj["validTimeInterval"]["end"]["date"]
    assert end.startswith(str((latest_end + PASS_EXPIRATION_GRACE_PERIOD).year))

    price_modules = [m for m in obj["textModulesData"] if m["id"] == "price"]
    assert len(price_modules) == 1
    assert price_modules[0]["body"] == "EUR 50.00"  # held_series_pass fixture price_paid


def test_series_pass_falls_back_to_latest_past_event(
    google_wallet_configured_settings: None,
    organization: Organization,
    google_event_series: EventSeries,
    google_series_pass: SeriesPass,
    member_user: RevelUser,
) -> None:
    """Once every covered event has ended, the representative is the latest-starting past one."""
    from events.utils import get_event_timezone
    from wallet.apple.formatting import format_iso_date
    from wallet.google.builder import build_series_pass_payload

    _covered_event(
        organization, google_event_series, google_series_pass, "GW Old One", "gw-old-one", timedelta(days=-14)
    )
    latest = _covered_event(
        organization, google_event_series, google_series_pass, "GW Old Two", "gw-old-two", timedelta(days=-7)
    )
    held = HeldSeriesPass.objects.create(
        series_pass=google_series_pass,
        user=member_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("30"),
    )

    payload = build_series_pass_payload(held)
    cls = payload["eventTicketClasses"][0]

    # Exact formatted timestamp, not just the year: the -14-day and -7-day fixture events
    # fall in the same year, so a year-only assertion can't tell "latest past" apart from
    # "earliest past" (i.e. a max->min regression in the representative-event selection).
    assert cls["dateTime"]["start"] == format_iso_date(latest.start, tz=get_event_timezone(latest))


def test_series_pass_no_covered_events_falls_back_to_created_at(
    google_wallet_configured_settings: None,
    google_series_pass: SeriesPass,
    member_user: RevelUser,
) -> None:
    """Defensive: a pass with no tier links still builds a valid payload from held_pass.created_at."""
    from wallet.google.builder import build_series_pass_payload

    held = HeldSeriesPass.objects.create(
        series_pass=google_series_pass,
        user=member_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("0"),
    )

    payload = build_series_pass_payload(held)
    cls = payload["eventTicketClasses"][0]

    assert cls["dateTime"]["start"].startswith(str(held.created_at.year))
    assert "heroImage" not in cls
