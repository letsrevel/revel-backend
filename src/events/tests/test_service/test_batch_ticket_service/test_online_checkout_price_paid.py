"""The online ``price_paid`` carve-out is permanent — pin it (#758, spec §5.5).

``should_stamp_price_paid`` is the single stamp authority for every checkout
branch except one: ``_online_checkout`` never asks it, and online tickets keep
``price_paid`` NULL **permanently**. The 1:1 ``Payment`` row is authoritative,
and ``Payment.amount`` is the amount actually charged — *net* for a
reverse-charge buyer — so copying it into ``price_paid`` would make the
column's meaning depend on the buyer's VAT status (decision on #758, option a).

These tests are the assertion half of that decision: a future reader must not
"fix" a NULL online row by stamping, and the money-bearing read path must keep
resolving an online ticket's price from its ``Payment`` row, never from the
tier or seat.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    Payment,
    PriceCategory,
    Ticket,
    TicketTier,
    VenueSeat,
    VenueSector,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating.pricing import should_stamp_price_paid
from events.tests.test_service.test_batch_ticket_service.conftest import PREMIUM, make_category_tier
from wallet.apple.generator import ApplePassGenerator

pytestmark = pytest.mark.django_db

FLAT_ONLINE_PRICE = Decimal("50.00")


@pytest.fixture
def online_event(organization: Organization) -> Event:
    """Open public event on a Stripe-connected org."""
    organization.stripe_account_id = "acct_online_758"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return Event.objects.create(
        organization=organization,
        name="Online Carveout Event",
        slug="online-carveout-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        max_tickets_per_user=5,
    )


def test_online_checkout_leaves_price_paid_null_and_payment_row_carries_the_amount(
    online_event: Event, member_user: RevelUser
) -> None:
    """A plain online sale: NULL ``price_paid``, one PENDING Payment per ticket with the amount.

    The NULL is not "unknown" and not a bug — it is the permanent carve-out: the
    amount lives on the ``Payment`` row, whose value can legitimately differ from
    any tier price (net for reverse charge). The wallet pass — the reader that can
    see online rows — resolves the price from that row.
    """
    tier = TicketTier.objects.create(
        event=online_event,
        name="Online Flat",
        price=FLAT_ONLINE_PRICE,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=50,
        max_tickets_per_user=5,
    )

    result = BatchTicketService(online_event, tier, member_user).create_batch(
        [TicketPurchaseItem(guest_name="Guest 1"), TicketPurchaseItem(guest_name="Guest 2")]
    )

    assert isinstance(result, tuple), "an ONLINE tier returns (tickets, reservation_id)"
    tickets, _reservation_id = result
    assert len(tickets) == 2
    for ticket in Ticket.objects.filter(tier=tier).select_related("tier", "seat"):
        assert ticket.status == Ticket.TicketStatus.PENDING
        assert ticket.price_paid is None, "online tickets must NEVER stamp price_paid (#758)"
        payment = Payment.objects.get(ticket=ticket)
        assert payment.amount == FLAT_ONLINE_PRICE
        # The money-bearing read path resolves via the Payment row.
        price, currency = ApplePassGenerator._resolve_price(ticket)
        assert (price, currency) == (payment.amount, "EUR")


def test_online_checkout_never_asks_the_stamp_authority_even_when_it_says_true(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """Category-priced ONLINE tier: the stamp authority says True, online still leaves NULL.

    This is the exact shape of the carve-out — ``should_stamp_price_paid`` returns
    True for a category-priced tier, and every other branch stamps. Online does not,
    because the Payment row already carries the charged amount, and the reader
    prefers that row over re-resolving the seat: mutating ``Payment.amount`` to a
    sentinel (the shape a reverse-charge *net* amount takes — different from the
    seat's gross 80.00) must move what the reader reports.
    """
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)
    assert should_stamp_price_paid(tier) is True, "a category-priced tier demands a stamp everywhere else"

    premium_seat = seats[0]
    result = BatchTicketService(seated_event, tier, member_user).create_batch(
        [TicketPurchaseItem(guest_name="Guest", seat_id=premium_seat.pk)]
    )

    assert isinstance(result, tuple)
    ticket = Ticket.objects.select_related("tier", "seat").get(tier=tier)
    assert ticket.seat_id == premium_seat.pk
    assert ticket.price_paid is None, "the carve-out is permanent — online never stamps (#758)"

    payment = Payment.objects.get(ticket=ticket)
    assert payment.amount == PREMIUM, "domestic buyer: charged the seat's gross category price"

    # Reader precedence: Payment beats seat resolution. A reverse-charge buyer's
    # Payment.amount is net (< the seat's gross price); the reader must report what
    # was charged, not what the seat lists.
    net_amount = Decimal("64.00")
    Payment.objects.filter(pk=payment.pk).update(amount=net_amount)
    fresh_ticket = Ticket.objects.select_related("tier", "seat").get(pk=ticket.pk)
    price, currency = ApplePassGenerator._resolve_price(fresh_ticket)
    assert (price, currency) == (net_amount, "EUR"), "the reader must consult the Payment row first"
    assert price != PREMIUM, "falling back to the seat's price would misreport a reverse-charge sale"
