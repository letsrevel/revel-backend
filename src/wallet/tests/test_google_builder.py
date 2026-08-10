"""Tests for the Google Wallet payload builder."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
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
)
from wallet.apple.formatting import get_theme_hex_background
from wallet.google.builder import build_ticket_payload

pytestmark = pytest.mark.django_db


@pytest.fixture
def google_wallet_settings(settings: t.Any) -> None:
    """Configure Google Wallet settings for tests."""
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = "/path/sa.json"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "test"


def test_ticket_payload_shape(google_wallet_settings: None, ticket: Ticket) -> None:
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


def test_ticket_payload_price_module(google_wallet_settings: None, ticket: Ticket) -> None:
    payload = build_ticket_payload(ticket)
    obj = payload["eventTicketObjects"][0]
    price_modules = [m for m in obj["textModulesData"] if m["id"] == "price"]
    assert len(price_modules) == 1
    assert price_modules[0]["body"] == "EUR 10.00"  # tier fixture price


def test_ticket_payload_address_only_event(google_wallet_settings: None, ticket: Ticket) -> None:
    """Event fixture has address='123 Test Street' and no venue: both venue
    sub-fields fall back to the address."""
    payload = build_ticket_payload(ticket)
    cls = payload["eventTicketClasses"][0]
    assert cls["venue"]["name"]["defaultValue"]["value"] == "123 Test Street"
    assert cls["venue"]["address"]["defaultValue"]["value"] == "123 Test Street"


def test_ticket_payload_no_holder_name(google_wallet_settings: None, ticket: Ticket) -> None:
    ticket.guest_name = ""
    payload = build_ticket_payload(ticket)
    assert "ticketHolderName" not in payload["eventTicketObjects"][0]


def test_ticket_payload_valid_time_interval(google_wallet_settings: None, ticket: Ticket) -> None:
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


def test_series_pass_payload(google_wallet_settings: None, held_series_pass: t.Any) -> None:
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
