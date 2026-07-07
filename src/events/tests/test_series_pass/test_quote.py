"""Tests for series pass price quote calculation."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from events.models import Event, EventSeries, Organization, SeriesPass, SeriesPassTierLink, TicketTier
from events.service.series_pass_service import get_quote


@pytest.fixture
def events_6_with_times(event_series: EventSeries, organization: Organization) -> list[Event]:
    """Create 6 events at 1-day intervals starting from a fixed time."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    events = []
    for i in range(6):
        start = base_time + timedelta(days=i)
        event = Event.objects.create(
            organization=organization,
            name=f"Event {i + 1}",
            slug=f"event-{i + 1}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=start,
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        events.append(event)
    return events


@pytest.fixture
def tiers_for_events(events_6_with_times: list[Event]) -> list[TicketTier]:
    """Create tiers for each of the 6 events."""
    tiers = []
    for event in events_6_with_times:
        tier = TicketTier.objects.create(
            event=event,
            name=f"Tier for {event.name}",
            price=Decimal("10.00"),
            currency="GBP",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        tiers.append(tier)
    return tiers


@pytest.fixture
def series_pass_with_6_events(
    series_pass: SeriesPass, events_6_with_times: list[Event], tiers_for_events: list[TicketTier]
) -> SeriesPass:
    """Link series pass to 6 events. Price 36.00, discount 6.00 (pro-rata per event)."""
    series_pass.price = Decimal("36.00")
    series_pass.pro_rata_discount = Decimal("6.00")
    series_pass.currency = "GBP"
    series_pass.save()

    for event, tier in zip(events_6_with_times, tiers_for_events):
        SeriesPassTierLink.objects.create(
            series_pass=series_pass,
            event=event,
            tier=tier,
        )
    return series_pass


def test_quote_before_event_1(series_pass_with_6_events: SeriesPass) -> None:
    """Before event 1 → price 36.00, 6 remaining, purchasable."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time - timedelta(hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.price == Decimal("36.00")
    assert quote.passed_events == 0
    assert quote.remaining_events == 6
    assert quote.currency == "GBP"
    assert quote.purchasable is True
    assert quote.reason is None


def test_quote_after_event_1(series_pass_with_6_events: SeriesPass) -> None:
    """After event 1 → price 30.00 (36 - 1*6)."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time + timedelta(hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.price == Decimal("30.00")
    assert quote.passed_events == 1
    assert quote.remaining_events == 5
    assert quote.purchasable is True


def test_quote_after_event_4(series_pass_with_6_events: SeriesPass) -> None:
    """After event 4 → price 12.00 (36 - 4*6)."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time + timedelta(days=3, hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.price == Decimal("12.00")
    assert quote.passed_events == 4
    assert quote.remaining_events == 2
    assert quote.purchasable is True


def test_quote_after_event_5_not_purchasable(series_pass_with_6_events: SeriesPass) -> None:
    """After event 5 (1 remaining) → not purchasable, reason mentions remaining events."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time + timedelta(days=4, hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.passed_events == 5
    assert quote.remaining_events == 1
    assert quote.purchasable is False
    assert quote.reason is not None
    assert "remaining" in quote.reason.lower()


def test_quote_price_clamped_to_zero(
    series_pass: SeriesPass, events_6_with_times: list[Event], tiers_for_events: list[TicketTier]
) -> None:
    """Price 10, discount 6, 3 passed → Decimal('0.00')."""
    series_pass.price = Decimal("10.00")
    series_pass.pro_rata_discount = Decimal("6.00")
    series_pass.currency = "GBP"
    series_pass.save()

    for event, tier in zip(events_6_with_times, tiers_for_events):
        SeriesPassTierLink.objects.create(
            series_pass=series_pass,
            event=event,
            tier=tier,
        )

    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time + timedelta(days=2, hours=1)

    quote = get_quote(series_pass, now=now)

    assert quote.price == Decimal("0.00")
    assert quote.passed_events == 3


def test_quote_is_active_false(series_pass_with_6_events: SeriesPass) -> None:
    """is_active=False → not purchasable."""
    series_pass_with_6_events.is_active = False
    series_pass_with_6_events.save()

    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time - timedelta(hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.purchasable is False
    assert quote.reason is not None


def test_quote_sales_end_at_in_past(series_pass_with_6_events: SeriesPass) -> None:
    """sales_end_at in past → not purchasable."""
    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time

    series_pass_with_6_events.sales_end_at = now - timedelta(hours=1)
    series_pass_with_6_events.save()

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.purchasable is False
    assert quote.reason is not None


def test_quote_sold_out(series_pass_with_6_events: SeriesPass) -> None:
    """total_quantity=quantity_sold → not purchasable (sold out reason)."""
    series_pass_with_6_events.total_quantity = 10
    series_pass_with_6_events.quantity_sold = 10
    series_pass_with_6_events.save()

    base_time = timezone.make_aware(datetime(2026, 7, 15, 18, 0, 0))
    now = base_time - timedelta(hours=1)

    quote = get_quote(series_pass_with_6_events, now=now)

    assert quote.purchasable is False
    assert quote.reason is not None
    assert "sold out" in quote.reason.lower()
