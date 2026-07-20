"""The free-checkout path must honour the ``price_paid`` stamp decision (spec §5.5).

``create_batch`` computes ``should_stamp_price_paid`` once and hands it to the
offline / at-the-door branches. The zeroed-ONLINE reroute used to drop it on the
floor, leaving ``price_paid`` NULL on a ticket that cost 0.00 — and a NULL is a
positive claim that ``tier.price`` reconstructs the sale, which for a
fully-discounted ticket is a false 25.00.

The FREE **payment method** is deliberately unchanged: it collects nothing, so the
NULL claim is true there and the price vector it carries was never charged.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

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
