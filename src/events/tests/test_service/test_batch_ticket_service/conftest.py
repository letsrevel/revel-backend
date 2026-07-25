"""Shared fixtures for BatchTicketService tests."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    PriceCategory,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

PREMIUM = Decimal("80.00")
STANDARD = Decimal("30.00")
FLAT = Decimal("50.00")


@pytest.fixture
def batch_event(organization: Organization) -> Event:
    """Future-dated public event used across batch-service tests."""
    return Event.objects.create(
        organization=organization,
        name="Batch Test Event",
        slug="batch-test-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=5,
    )


@pytest.fixture
def batch_offline_tier(batch_event: Event) -> TicketTier:
    """Offline-payment tier on the batch test event."""
    return TicketTier.objects.create(
        event=batch_event,
        name="Offline Entry",
        price=Decimal("25.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=100,
    )


@pytest.fixture
def batch_user(member_user: RevelUser) -> RevelUser:
    """Alias for the buyer user in batch-service tests."""
    return member_user


@pytest.fixture
def run_checkout_offline(
    batch_event: Event,
    batch_offline_tier: TicketTier,
    batch_user: RevelUser,
) -> t.Callable[..., list[Ticket]]:
    """Run BatchTicketService.create_batch() against the offline tier with N guests."""

    def _run(count: int = 1) -> list[Ticket]:
        service = BatchTicketService(batch_event, batch_offline_tier, batch_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i + 1}") for i in range(count)]
        result = service.create_batch(items)
        assert isinstance(result, list)
        return result

    return _run


# ---------------------------------------------------------------------------
# Category-priced seating (#739) — shared by the mixed-cart pricing test modules
# ---------------------------------------------------------------------------


@pytest.fixture
def seated_org(organization: Organization) -> Organization:
    """Stripe-connected org with a 3% + 0.50 platform fee."""
    organization.stripe_account_id = "acct_seated"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def seated_event(seated_org: Organization) -> Event:
    """Open public event with room for the whole cart."""
    return Event.objects.create(
        organization=seated_org,
        name="Seated Event",
        slug="seated-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        max_tickets_per_user=5,
    )


@pytest.fixture
def sector(seated_org: Organization, seated_event: Event) -> VenueSector:
    """A sector in a venue that has Premium and Standard categories."""
    venue = Venue.objects.create(organization=seated_org, name="Theatre", capacity=100)
    seated_event.venue = venue
    seated_event.save(update_fields=["venue"])
    return VenueSector.objects.create(
        venue=venue,
        name="Stalls",
        shape=[{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10}],
    )


@pytest.fixture
def categories(sector: VenueSector) -> tuple[PriceCategory, PriceCategory]:
    """Premium and Standard, painted onto the sector's seats below."""
    premium = PriceCategory.objects.create(venue=sector.venue, name="Premium", color="#aa0000")
    standard = PriceCategory.objects.create(venue=sector.venue, name="Standard", color="#00aa00")
    return premium, standard


@pytest.fixture
def seats(sector: VenueSector, categories: tuple[PriceCategory, PriceCategory]) -> list[VenueSeat]:
    """A1 Premium, A2 Standard, A3 unpainted (falls back to the flat tier price)."""
    premium, standard = categories
    painted: list[PriceCategory | None] = [premium, standard, None]
    return [
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{i + 1}",
            row_label="A",
            number=i + 1,
            position={"x": i, "y": 0},
            is_active=True,
            default_price_category=category,
        )
        for i, category in enumerate(painted)
    ]


def make_category_tier(
    event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    method: TicketTier.PaymentMethod,
    *,
    prices: tuple[Decimal, Decimal] = (PREMIUM, STANDARD),
    flat: Decimal = FLAT,
) -> TicketTier:
    """Create a user-choice tier that prices Premium and Standard from its category map."""
    premium, standard = categories
    return TicketTier.objects.create(
        event=event,
        name=f"Stalls {method}",
        price=flat,
        currency="EUR",
        payment_method=method,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=sector.venue,
        sector=sector,
        category_prices={str(premium.pk): str(prices[0]), str(standard.pk): str(prices[1])},
    )
