"""The free-checkout path must honour the ``price_paid`` stamp decision (spec §5.5).

``create_batch`` computes ``should_stamp_price_paid`` once and hands it to the
offline / at-the-door branches. The zeroed-ONLINE reroute used to drop it on the
floor, leaving ``price_paid`` NULL on a ticket that cost 0.00 — and a NULL is a
positive claim that ``tier.price`` reconstructs the sale, which for a
fully-discounted ticket is a false 25.00.

The ``case FREE:`` branch dropped it too, which bit a *category-priced* free tier:
``should_stamp_price_paid`` says True there (no tier price reconstructs a seat price),
the branch passed nothing, and the row landed NULL — the very state the function's
contract says must not exist. What gets recorded on this path is ``0.00``, never the
price vector's list price, mirroring the box-office comp: a giveaway must not report
tier-price revenue whatever the seat is worth.

A plain free tier is deliberately unchanged: no map, no buyer input, so
``should_stamp_price_paid`` says False and the NULL claim is true.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, PriceCategory, Ticket, TicketTier, VenueSeat, VenueSector
from events.models.discount_code import DiscountCode
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating.pricing import recorded_or_resolved_price
from events.tests.test_service.test_batch_ticket_service.conftest import PREMIUM, make_category_tier
from wallet.apple.generator import ApplePassGenerator

pytestmark = pytest.mark.django_db

TIER_PRICE = Decimal("25.00")


@pytest.fixture
def zero_cart_event(organization: Organization) -> Event:
    """Open public event with room for the cart."""
    return Event.objects.create(
        organization=organization,
        name="Zero Cart Event",
        slug="zero-cart-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        max_tickets_per_user=5,
    )


def _tier(event: Event, method: TicketTier.PaymentMethod, price: Decimal) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=f"Tier {method}",
        price=price,
        currency="EUR",
        payment_method=method,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=50,
        max_tickets_per_user=5,
    )


def _items(count: int = 2) -> list[TicketPurchaseItem]:
    return [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(count)]


def test_fully_discounted_online_cart_stamps_price_paid(
    zero_cart_event: Event, organization: Organization, member_user: RevelUser
) -> None:
    """A code worth the whole ticket reroutes an ONLINE cart to free — and records the 0.00.

    There is no ``Payment`` row on this path, so ``price_paid`` is the only place
    the amount can live. Leaving it NULL made every reader that falls back
    (the admin ticket list's ``EFFECTIVE_PRICE_PAID``, the Apple Wallet pass)
    report the full ``tier.price`` for a ticket the buyer got for nothing.
    """
    tier = _tier(zero_cart_event, TicketTier.PaymentMethod.ONLINE, TIER_PRICE)
    code = DiscountCode.objects.create(
        code="ZEROOUT",
        organization=organization,
        discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
        discount_value=TIER_PRICE,
        currency="EUR",
        max_uses_per_user=10,
    )

    result = BatchTicketService(zero_cart_event, tier, member_user, discount_code=code).create_batch(_items())

    assert isinstance(result, list), "a zeroed ONLINE cart returns tickets, not a reservation"
    assert len(result) == 2
    assert not Payment.objects.filter(ticket__tier=tier).exists()
    for ticket in Ticket.objects.filter(tier=tier):
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.price_paid == Decimal("0.00")
        assert ticket.discount_amount == TIER_PRICE


def test_genuinely_free_tier_leaves_price_paid_null(zero_cart_event: Event, member_user: RevelUser) -> None:
    """A FREE-payment-method tier at price 0 with no buyer input still stamps nothing.

    Nothing is collected, so the NULL "``tier.price`` reconstructs this" claim is
    true. Pinned so the zero-cart fix above is not mistaken for a licence to stamp
    on every free ticket.
    """
    tier = _tier(zero_cart_event, TicketTier.PaymentMethod.FREE, Decimal("0.00"))

    result = BatchTicketService(zero_cart_event, tier, member_user).create_batch(_items())

    assert isinstance(result, list)
    assert len(result) == 2
    for ticket in Ticket.objects.filter(tier=tier):
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.price_paid is None
        assert ticket.discount_amount is None


def test_free_comp_on_a_category_priced_tier_records_zero_not_null(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """A free ticket for an 80.00 Premium seat is 0.00 everywhere, not "€80.00" on a phone.

    ``should_stamp_price_paid`` returns True for a category-priced tier, but the
    ``case FREE:`` branch dropped the argument, so the row landed ``price_paid IS NULL``.
    ``recorded_or_resolved_price`` then logged the anomaly and priced the ticket **from
    the seat** — which is how the Apple Wallet pass came to print ``€80.00`` on a
    giveaway. Both readers must now say 0.00, and they must say the *same* 0.00.
    """
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.FREE)
    premium_seat = seats[0]

    result = BatchTicketService(seated_event, tier, member_user).create_batch(
        [TicketPurchaseItem(guest_name="Comped Guest", seat_id=premium_seat.pk)]
    )

    assert isinstance(result, list)
    ticket = Ticket.objects.select_related("tier", "seat").get(pk=result[0].pk)
    assert ticket.seat_id == premium_seat.pk
    assert ticket.price_paid == Decimal("0.00"), "a comp must not carry the seat's list price, nor a NULL"

    # The two money-bearing readers the NULL used to mislead.
    assert recorded_or_resolved_price(ticket.tier, ticket.seat, ticket.price_paid) == Decimal("0.00")
    price, currency = ApplePassGenerator._resolve_price(ticket)
    assert (price, currency) == (Decimal("0.00"), "EUR")
    assert price != PREMIUM, "the wallet pass printed the seat's 80.00 before the fix"
