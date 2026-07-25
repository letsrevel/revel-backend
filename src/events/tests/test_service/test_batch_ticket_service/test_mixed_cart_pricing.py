"""The per-ticket price vector reaching tickets, Payments and the platform fee (plan Task 6).

No tier reachable through the buyer API carries a ``category_prices`` map yet
(that is Task 10), so these tests build the mixed cart by driving
``BatchTicketService.create_batch`` directly against a tier whose map is set here.

What is pinned:

- ``Ticket.price_paid`` per ticket — the seat's own resolved price, and **NULL on
  the online path**, where ``Payment.amount`` is authoritative (spec §5.5).
- ``Ticket.discount_amount`` per ticket — 8.00 and 3.00 on an 80/30 cart with a
  10% code, not one tier-wide scalar; and **NULL** (not ``0.00``) with no code.
- ``Payment.amount`` per ticket — one row each, at its own price.
- The platform fee — derived from the **true total** (sum of the rounded
  per-ticket prices), so round-then-sum survives all the way to the fee's
  ``ROUND_HALF_UP``.
"""

from decimal import Decimal
from unittest import mock

import pytest

from accounts.models import RevelUser
from events.models import (
    DiscountCode,
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
from events.service import stripe_service
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating.pricing import TicketPrice
from events.tests.test_service.test_batch_ticket_service.conftest import (
    FLAT,
    PREMIUM,
    STANDARD,
)
from events.tests.test_service.test_batch_ticket_service.conftest import (
    make_category_tier as _make_tier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced ONLINE tier: Premium 80, Standard 30, unpainted 50."""
    return _make_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)


@pytest.fixture
def offline_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced OFFLINE tier with the same map."""
    return _make_tier(seated_event, sector, categories, TicketTier.PaymentMethod.OFFLINE)


@pytest.fixture
def pct10(seated_org: Organization) -> DiscountCode:
    """10% off — 80.00 → 72.00 (‑8.00) and 30.00 → 27.00 (‑3.00)."""
    return DiscountCode.objects.create(
        code="PCT10",
        organization=seated_org,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        currency="EUR",
        max_uses_per_user=10,
    )


def _items(seats: list[VenueSeat]) -> list[TicketPurchaseItem]:
    return [TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seat.pk) for i, seat in enumerate(seats)]


def _reserve(
    event: Event, tier: TicketTier, user: RevelUser, seats: list[VenueSeat], code: DiscountCode | None = None
) -> list[Payment]:
    """Drive an ONLINE cart through create_batch and return its Payment rows in cart order."""
    service = BatchTicketService(event, tier, user, discount_code=code)
    result = service.create_batch(_items(seats))
    assert isinstance(result, tuple)
    tickets, reservation_id = result
    payments = list(Payment.objects.filter(reservation_id=reservation_id).select_related("ticket"))
    order = {ticket.pk: i for i, ticket in enumerate(tickets)}
    return sorted(payments, key=lambda payment: order[payment.ticket_id])


class TestOnlineMixedCart:
    """ONLINE: one Payment per ticket at its own price; price_paid stays NULL."""

    def test_each_payment_carries_its_own_seat_price(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """Premium 80 + Standard 30 + unpainted 50 — not three times lines[0]."""
        payments = _reserve(seated_event, online_tier, member_user, seats)

        assert [payment.amount for payment in payments] == [PREMIUM, STANDARD, FLAT]
        assert len(payments) == 3  # 1:1 with tickets

    def test_online_tickets_keep_price_paid_null(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """Spec §5.5: Payment.amount is authoritative online, so price_paid stays NULL."""
        payments = _reserve(seated_event, online_tier, member_user, seats)

        assert [payment.ticket.price_paid for payment in payments] == [None, None, None]
        assert [payment.ticket.discount_amount for payment in payments] == [None, None, None]

    def test_platform_fee_comes_from_the_true_total(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """3% of 160.00 (+0.50 fixed), not 3% of 80.00 × 3."""
        payments = _reserve(seated_event, online_tier, member_user, seats)

        assert sum(payment.amount for payment in payments) == Decimal("160.00")
        # fee_net = 160.00 * 3% + 0.50 = 5.30; the per-ticket split sums back to it.
        assert sum((payment.platform_fee_net or Decimal("0") for payment in payments), Decimal("0")) == Decimal("5.30")

    def test_discount_lands_per_ticket_on_both_the_ticket_and_the_payment(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        pct10: DiscountCode,
    ) -> None:
        """Spec §5.3: the true discounts are 8.00 / 3.00 / 5.00, not one scalar."""
        payments = _reserve(seated_event, online_tier, member_user, seats, code=pct10)

        assert [payment.amount for payment in payments] == [Decimal("72.00"), Decimal("27.00"), Decimal("45.00")]
        assert [payment.ticket.discount_amount for payment in payments] == [
            Decimal("8.00"),
            Decimal("3.00"),
            Decimal("5.00"),
        ]

    def test_a_zero_priced_ticket_in_a_mixed_cart_still_gets_a_payment_row(
        self,
        seated_event: Event,
        sector: VenueSector,
        categories: tuple[PriceCategory, PriceCategory],
        seats: list[VenueSeat],
        seated_org: Organization,
        member_user: RevelUser,
    ) -> None:
        """A code that zeroes the cheap seat must not break the 1:1 ticket↔Payment pairing."""
        tier = _make_tier(
            seated_event,
            sector,
            categories,
            TicketTier.PaymentMethod.ONLINE,
            prices=(PREMIUM, Decimal("30.00")),
        )
        code = DiscountCode.objects.create(
            code="FIX30",
            organization=seated_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("30.00"),
            currency="EUR",
            max_uses_per_user=10,
        )

        payments = _reserve(seated_event, tier, member_user, seats[:2], code=code)

        assert [payment.amount for payment in payments] == [Decimal("50.00"), Decimal("0.00")]
        assert [payment.ticket.status for payment in payments] == [Ticket.TicketStatus.PENDING] * 2


class TestRoundingReachesThePlatformFee:
    """Round per ticket, then sum — and the fee rounds ROUND_HALF_UP on that sum."""

    def test_three_tickets_at_a_third_off_total_2001_not_2000(
        self,
        seated_event: Event,
        seated_org: Organization,
        sector: VenueSector,
        categories: tuple[PriceCategory, PriceCategory],
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """3 × 10.00 at 33.33% → 6.67 each → 20.01. Summing first would give 20.00.

        The org charges 50% here so the cent is visible in the fee: 50% of 20.01
        is 10.005, which ROUND_HALF_UP takes to **10.01**; 50% of 20.00 is 10.00.
        """
        seated_org.platform_fee_percent = Decimal("50.00")
        seated_org.platform_fee_fixed = Decimal("0.00")
        seated_org.save()
        tier = _make_tier(
            seated_event,
            sector,
            categories,
            TicketTier.PaymentMethod.ONLINE,
            prices=(Decimal("10.00"), Decimal("10.00")),
            flat=Decimal("10.00"),
        )
        code = DiscountCode.objects.create(
            code="THIRD",
            organization=seated_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("33.33"),
            currency="EUR",
            max_uses_per_user=10,
        )

        payments = _reserve(seated_event, tier, member_user, seats, code=code)

        assert [payment.amount for payment in payments] == [Decimal("6.67")] * 3
        total = sum(payment.amount for payment in payments)
        assert total == Decimal("20.01")
        assert sum((payment.platform_fee_net or Decimal("0") for payment in payments), Decimal("0")) == Decimal("10.01")


class TestOfflineMixedCart:
    """OFFLINE: the seat's resolved price lands on ``price_paid``, per ticket."""

    def test_price_paid_is_per_seat_and_discount_amount_is_null_without_a_code(
        self, seated_event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """``discount_amount`` stays NULL — the vector's 0.00 must not be stamped."""
        service = BatchTicketService(seated_event, offline_tier, member_user)

        tickets = service.create_batch(_items(seats))

        assert isinstance(tickets, list)
        assert [ticket.price_paid for ticket in tickets] == [PREMIUM, STANDARD, FLAT]
        assert [ticket.discount_amount for ticket in tickets] == [None, None, None]

    def test_discounted_price_paid_and_discount_amount_are_both_per_ticket(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        pct10: DiscountCode,
    ) -> None:
        """80/30/50 at 10% off → paid 72/27/45, discounted 8/3/5."""
        service = BatchTicketService(seated_event, offline_tier, member_user, discount_code=pct10)

        tickets = service.create_batch(_items(seats))

        assert isinstance(tickets, list)
        assert [ticket.price_paid for ticket in tickets] == [Decimal("72.00"), Decimal("27.00"), Decimal("45.00")]
        assert [ticket.discount_amount for ticket in tickets] == [Decimal("8.00"), Decimal("3.00"), Decimal("5.00")]


class TestVATIsMemoisedPerPrice:
    """VAT is arithmetic under the tier lock — one computation per *distinct* price."""

    def test_attendee_vat_runs_once_per_distinct_price(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """Four tickets across two distinct prices resolve VAT twice, not four times.

        ``_attendee_vat_from_context`` is deliberately network-free (the VIES
        round-trip happened pre-lock), so this is about not doing avoidable work
        under the contended row — and about making a per-ticket lookup structurally
        impossible to reintroduce.
        """
        from events.service.attendee_vat_service import BuyerVATContext

        tickets = [
            Ticket.objects.create(
                event=seated_event,
                tier=online_tier,
                user=member_user,
                status=Ticket.TicketStatus.PENDING,
                guest_name=f"Guest {i}",
            )
            for i in range(4)
        ]
        lines = [
            TicketPrice(unit_price=price, discount_amount=Decimal("0.00"))
            for price in (PREMIUM, STANDARD, PREMIUM, STANDARD)
        ]

        with mock.patch.object(
            stripe_service, "_attendee_vat_from_context", wraps=stripe_service._attendee_vat_from_context
        ) as vat:
            stripe_service.reserve_batch_payments(
                event=seated_event,
                tier=online_tier,
                user=member_user,
                tickets=tickets,
                reservation_id=seated_event.pk,
                lines=lines,
                buyer_vat_context=BuyerVATContext(buyer_country="AT", buyer_vat_validated=False),
            )

        assert vat.call_count == 2
        amounts = [payment.amount for payment in Payment.objects.filter(ticket__in=tickets).order_by("ticket__id")]
        assert sorted(amounts) == sorted([PREMIUM, STANDARD, PREMIUM, STANDARD])
