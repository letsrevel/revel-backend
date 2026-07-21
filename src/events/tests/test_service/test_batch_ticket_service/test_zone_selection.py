"""End-to-end best-available purchase on a two-zone tier (#749).

The buyer names the zone per request; the picker draws only from it and the sale
records THAT zone's mapped price, on every payment method. Pricing itself is
``events.service.seating.pricing``'s rule — these tests only pin that the zone the
buyer chose is the zone that gets charged.
"""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    Payment,
    PriceCategory,
    Ticket,
    TicketTier,
    VenueSeat,
    VenueSector,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.tests.test_service.test_batch_ticket_service.conftest import (
    FLAT,
    PREMIUM,
    STANDARD,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def zoned_seats(sector: VenueSector, categories: tuple[PriceCategory, PriceCategory]) -> list[VenueSeat]:
    """Four seats: A1/A2 Premium, A3/A4 Standard — one adjacent pair per zone."""
    premium, standard = categories
    painted = [premium, premium, standard, standard]
    return [
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{i + 1}",
            row_label="A",
            number=i + 1,
            adjacency_index=i,
            position={"x": i, "y": 0},
            is_active=True,
            default_price_category=category,
        )
        for i, category in enumerate(painted)
    ]


def _ba_tier(
    event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    method: TicketTier.PaymentMethod,
) -> TicketTier:
    premium, standard = categories
    # The best-available pool is read off the event's venue, so bind it.
    event.venue = sector.venue
    event.save(update_fields=["venue"])
    return TicketTier.objects.create(
        event=event,
        name=f"Zoned {method}",
        price=FLAT,
        currency="EUR",
        payment_method=method,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        venue=sector.venue,
        sector=sector,
        category_prices={str(premium.pk): str(PREMIUM), str(standard.pk): str(STANDARD)},
    )


def _items(count: int) -> list[TicketPurchaseItem]:
    return [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(count)]


@pytest.mark.parametrize(
    "method",
    [TicketTier.PaymentMethod.OFFLINE, TicketTier.PaymentMethod.AT_THE_DOOR],
)
@pytest.mark.parametrize(
    ("zone_index", "expected_price"),
    [(0, PREMIUM), (1, STANDARD)],
)
def test_instant_issue_charges_the_chosen_zone_price(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    zoned_seats: list[VenueSeat],
    member_user: RevelUser,
    method: TicketTier.PaymentMethod,
    zone_index: int,
    expected_price: Decimal,
) -> None:
    """Both instant-issue methods stamp the mapped price of the zone the buyer picked."""
    tier = _ba_tier(seated_event, sector, categories, method)
    zone = categories[zone_index]

    tickets = BatchTicketService(seated_event, tier, member_user, price_category_id=zone.pk).create_batch(
        items=_items(2)
    )

    assert isinstance(tickets, list)
    assert {ticket.seat.default_price_category_id for ticket in tickets if ticket.seat} == {zone.pk}
    assert [ticket.price_paid for ticket in tickets] == [expected_price, expected_price]


@pytest.mark.parametrize(
    ("zone_index", "expected_price"),
    [(0, PREMIUM), (1, STANDARD)],
)
def test_online_reservation_charges_the_chosen_zone_price(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    zoned_seats: list[VenueSeat],
    member_user: RevelUser,
    zone_index: int,
    expected_price: Decimal,
) -> None:
    """ONLINE: Payment.amount is authoritative, and it is the chosen zone's price."""
    tier = _ba_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)
    zone = categories[zone_index]

    result = BatchTicketService(seated_event, tier, member_user, price_category_id=zone.pk).create_batch(
        items=_items(2)
    )

    assert isinstance(result, tuple)
    tickets, reservation_id = result
    payments = list(Payment.objects.filter(reservation_id=reservation_id))
    assert [payment.amount for payment in payments] == [expected_price, expected_price]
    assert {ticket.seat.default_price_category_id for ticket in tickets if ticket.seat} == {zone.pk}


def test_free_tier_assigns_from_the_chosen_zone(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    zoned_seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """FREE: nothing to charge, but the zone still decides which seats are issued."""
    tier = _ba_tier(seated_event, sector, categories, TicketTier.PaymentMethod.FREE)
    _premium, standard = categories

    tickets = BatchTicketService(seated_event, tier, member_user, price_category_id=standard.pk).create_batch(
        items=_items(2)
    )

    assert isinstance(tickets, list)
    assert {ticket.status for ticket in tickets} == {Ticket.TicketStatus.ACTIVE}
    assert {ticket.seat_id for ticket in tickets} == {zoned_seats[2].pk, zoned_seats[3].pk}
