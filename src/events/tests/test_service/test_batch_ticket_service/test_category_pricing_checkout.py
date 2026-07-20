"""Checkout integration for category-priced tiers — the first reachable mixed cart (plan Task 10).

Tasks 1-9 built the map, the pricing core and the six money paths that were only
safe while every cart had one price. This module drives the whole thing end to end
through ``BatchTicketService.create_batch``, for **every** payment method, and pins
the three things Task 10 actually changes:

1. ``build_batch_pricing`` runs post-resolution, under the tier's
   ``select_for_update``, off the **locked** row — so an admin repricing mid-checkout
   is serialized against the sale rather than racing it.
2. A seat painted into a category the tier does not price is **refused**, not
   silently charged the flat price (decision 2026-07-20). Painting is venue-scoped
   and never hard-fails, so the gap has to bite here.
3. ``min_purchase_amount`` is enforced here against the real cart total, not
   pre-resolution against ``tier.price * batch_size``.

Cart shape throughout: seat A1 Premium 80.00, A2 Standard 30.00, A3 unpainted →
the tier's flat 50.00. Gross total 160.00.
"""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from ninja.errors import HttpError

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
from events.tests.test_service.test_batch_ticket_service.conftest import (
    FLAT,
    PREMIUM,
    STANDARD,
    make_category_tier,
)

pytestmark = pytest.mark.django_db

GROSS_TOTAL = PREMIUM + STANDARD + FLAT  # 160.00


def _items(seats: list[VenueSeat]) -> list[TicketPurchaseItem]:
    return [TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seat.pk) for i, seat in enumerate(seats)]


def _buy(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    seats: list[VenueSeat],
    code: DiscountCode | None = None,
) -> list[Ticket] | tuple[list[Ticket], t.Any]:
    return BatchTicketService(event, tier, user, discount_code=code).create_batch(_items(seats))


def _tickets(result: list[Ticket] | tuple[list[Ticket], t.Any]) -> list[Ticket]:
    return result[0] if isinstance(result, tuple) else result


def _payments_in_cart_order(tickets: list[Ticket]) -> list[Payment]:
    order = {ticket.pk: i for i, ticket in enumerate(tickets)}
    payments = Payment.objects.filter(ticket__in=tickets).select_related("ticket")
    return sorted(payments, key=lambda payment: order[payment.ticket_id])


@pytest.fixture
def online_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced ONLINE tier: Premium 80, Standard 30, unpainted 50."""
    return make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)


@pytest.fixture
def offline_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced OFFLINE tier with the same map."""
    return make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.OFFLINE)


@pytest.fixture
def door_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced AT_THE_DOOR tier with the same map."""
    return make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.AT_THE_DOOR)


@pytest.fixture
def free_tier(
    seated_event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """A FREE tier that nonetheless carries a category map (legal — the floor is ONLINE-only)."""
    return make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.FREE)


@pytest.fixture
def pct10(seated_org: Organization) -> DiscountCode:
    """10% off — 80 → 72 (‑8), 30 → 27 (‑3), 50 → 45 (‑5)."""
    return DiscountCode.objects.create(
        code="PCT10",
        organization=seated_org,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        currency="EUR",
        max_uses_per_user=10,
    )


class TestMixedCartByPaymentMethod:
    """Every payment method must price each seat from its own category."""

    def test_online_records_one_payment_per_ticket_at_its_own_price(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """80 / 30 / 50, and ``price_paid`` stays NULL (Payment.amount is authoritative — §5.5)."""
        tickets = _tickets(_buy(seated_event, online_tier, member_user, seats))
        payments = _payments_in_cart_order(tickets)

        assert [payment.amount for payment in payments] == [PREMIUM, STANDARD, FLAT]
        assert [ticket.price_paid for ticket in tickets] == [None, None, None]
        assert [ticket.discount_amount for ticket in tickets] == [None, None, None]
        assert sum(payment.amount for payment in payments) == GROSS_TOTAL

    def test_offline_stamps_price_paid_per_ticket(
        self, seated_event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """Offline has no Payment row, so the resolved price must land on the ticket itself."""
        tickets = _tickets(_buy(seated_event, offline_tier, member_user, seats))

        assert [ticket.price_paid for ticket in tickets] == [PREMIUM, STANDARD, FLAT]
        assert [ticket.discount_amount for ticket in tickets] == [None, None, None]
        assert [ticket.status for ticket in tickets] == [Ticket.TicketStatus.PENDING] * 3
        assert not Payment.objects.filter(ticket__in=tickets).exists()

    def test_at_the_door_stamps_price_paid_per_ticket_on_active_tickets(
        self, seated_event: Event, door_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """Door staff need the amount to collect per seat, not one tier-wide number."""
        tickets = _tickets(_buy(seated_event, door_tier, member_user, seats))

        assert [ticket.price_paid for ticket in tickets] == [PREMIUM, STANDARD, FLAT]
        assert [ticket.status for ticket in tickets] == [Ticket.TicketStatus.ACTIVE] * 3

    def test_free_tier_ignores_the_map_and_charges_nothing(
        self, seated_event: Event, free_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """A FREE tier is free whatever the map says: ACTIVE tickets, no price, no Payment rows.

        ``price_paid`` stays NULL because nothing was paid — the map is configuration
        for a paid tier and must not manufacture a charge on a free one.
        """
        tickets = _tickets(_buy(seated_event, free_tier, member_user, seats))

        assert [ticket.status for ticket in tickets] == [Ticket.TicketStatus.ACTIVE] * 3
        assert [ticket.price_paid for ticket in tickets] == [None, None, None]
        assert not Payment.objects.filter(ticket__in=tickets).exists()

    def test_a_code_that_zeroes_every_seat_reroutes_an_online_cart_to_free_checkout(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        seated_org: Organization,
    ) -> None:
        """The free shortcut is derived from the whole **vector** — 80 and 30 and 50 must all zero out."""
        full = DiscountCode.objects.create(
            code="ALLFREE",
            organization=seated_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("100.00"),
            currency="EUR",
            max_uses_per_user=10,
        )

        result = _buy(seated_event, online_tier, member_user, seats, code=full)

        assert isinstance(result, list), "an all-zero cart takes the free path, not the reservation tuple"
        assert [ticket.status for ticket in result] == [Ticket.TicketStatus.ACTIVE] * 3
        assert not Payment.objects.filter(ticket__in=result).exists()


class TestDiscountAcrossAMixedCart:
    """A percentage code applies per ticket — the discounts genuinely differ row to row."""

    def test_online_payments_and_ticket_discount_amounts_are_per_seat(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        pct10: DiscountCode,
    ) -> None:
        """72 / 27 / 45 charged; 8 / 3 / 5 recorded as the discount, not 3 × one scalar."""
        tickets = _tickets(_buy(seated_event, online_tier, member_user, seats, code=pct10))
        payments = _payments_in_cart_order(tickets)

        assert [payment.amount for payment in payments] == [Decimal("72.00"), Decimal("27.00"), Decimal("45.00")]
        assert [ticket.discount_amount for ticket in tickets] == [
            Decimal("8.00"),
            Decimal("3.00"),
            Decimal("5.00"),
        ]

    def test_offline_price_paid_and_discount_amount_are_both_per_seat(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        pct10: DiscountCode,
    ) -> None:
        """The revenue detail sheet reads both columns; both must be the true per-ticket numbers."""
        tickets = _tickets(_buy(seated_event, offline_tier, member_user, seats, code=pct10))

        assert [ticket.price_paid for ticket in tickets] == [Decimal("72.00"), Decimal("27.00"), Decimal("45.00")]
        assert [ticket.discount_amount for ticket in tickets] == [
            Decimal("8.00"),
            Decimal("3.00"),
            Decimal("5.00"),
        ]


class TestVATAndPlatformFeeOnTheTrueTotal:
    """Both are computed from the mixed total, never from ``unit × n``."""

    def test_vat_splits_per_seat_and_the_fee_comes_from_the_real_total(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """20% VAT-inclusive: 80 → 13.33, 30 → 5.00, 50 → 8.33. Fee: 3% of 160.00 + 0.50 = 5.30.

        Taking the first ticket's numbers ×3 would give 39.99 VAT and a 7.70 fee —
        both wrong, and both invisible without a mixed cart.
        """
        online_tier.vat_rate = Decimal("20.00")
        online_tier.save(update_fields=["vat_rate"])

        tickets = _tickets(_buy(seated_event, online_tier, member_user, seats))
        payments = _payments_in_cart_order(tickets)

        assert [payment.vat_amount for payment in payments] == [
            Decimal("13.33"),
            Decimal("5.00"),
            Decimal("8.33"),
        ]
        assert [payment.net_amount for payment in payments] == [
            Decimal("66.67"),
            Decimal("25.00"),
            Decimal("41.67"),
        ]
        # net + vat reconstructs each charged amount, and the whole cart's gross total.
        assert all(
            (payment.net_amount or Decimal("0")) + (payment.vat_amount or Decimal("0")) == payment.amount
            for payment in payments
        )
        assert sum(payment.amount for payment in payments) == GROSS_TOTAL
        # 160.00 * 3% + 0.50 = 5.30, split across the three rows.
        assert sum((payment.platform_fee_net or Decimal("0") for payment in payments), Decimal("0")) == Decimal("5.30")


class TestStripeSessionForAMixedCart:
    """The session must bill each ticket its own amount and reconcile against our books."""

    def test_line_items_are_distinct_and_the_session_total_matches_the_payment_rows(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """8000 / 3000 / 5000 minor units, summing to exactly ``sum(Payment.amount)``."""
        result = _buy(seated_event, online_tier, member_user, seats)
        assert isinstance(result, tuple)
        tickets, reservation_id = result

        fake = mock.Mock(id="cs_mixed_e2e", url="https://checkout.stripe.com/c/cs_mixed_e2e")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=reservation_id)
        line_items = create.call_args.kwargs["line_items"]

        charged = sorted(item["price_data"]["unit_amount"] * item["quantity"] for item in line_items)
        assert charged == [3000, 5000, 8000]
        recorded = sum((payment.amount for payment in _payments_in_cart_order(tickets)), Decimal("0"))
        assert sum(charged) == int(recorded * 100) == 16000


class TestUnpricedCategoryIsRefused:
    """A painted-but-unpriced seat must refuse, never fall back (decision 2026-07-20)."""

    @staticmethod
    def _repaint_to_a_new_unpriced_category(seat: VenueSeat, sector: VenueSector) -> PriceCategory:
        """Simulate the drift: the venue paints a category the tier's map never learned about."""
        balcony = PriceCategory.objects.create(venue=sector.venue, name="Balcony", color="#0000aa")
        seat.default_price_category = balcony
        seat.save(update_fields=["default_price_category"])
        return balcony

    def test_buying_an_unpriced_seat_400s_and_names_the_category(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        sector: VenueSector,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """Charging ``tier.price`` here is exactly the mispricing this feature exists to prevent."""
        self._repaint_to_a_new_unpriced_category(seats[1], sector)

        with pytest.raises(HttpError) as exc_info:
            _buy(seated_event, offline_tier, member_user, seats)

        assert exc_info.value.status_code == 400
        assert "Balcony" in str(exc_info.value.message)

    def test_the_refused_cart_creates_nothing(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        sector: VenueSector,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """The refusal happens under the tier lock, so the whole batch rolls back."""
        self._repaint_to_a_new_unpriced_category(seats[1], sector)
        before = offline_tier.quantity_sold

        with pytest.raises(HttpError):
            _buy(seated_event, offline_tier, member_user, seats)

        offline_tier.refresh_from_db()
        assert not Ticket.objects.filter(event=seated_event, tier=offline_tier).exists()
        assert offline_tier.quantity_sold == before

    def test_seats_in_priced_categories_still_sell(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        sector: VenueSector,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """The gap makes the *affected seats* unsellable — not the tier, and not the venue's map work."""
        self._repaint_to_a_new_unpriced_category(seats[1], sector)

        tickets = _tickets(_buy(seated_event, offline_tier, member_user, [seats[0], seats[2]]))

        assert [ticket.price_paid for ticket in tickets] == [PREMIUM, FLAT]


class TestMinPurchaseIsCheckedOnTheRealTotal:
    """§5.6 — the threshold moved to checkout, where the cart total is finally known."""

    @staticmethod
    def _code(org: Organization, minimum: str) -> DiscountCode:
        return DiscountCode.objects.create(
            code=f"MIN{minimum.replace('.', '')}",
            organization=org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            currency="EUR",
            min_purchase_amount=Decimal(minimum),
            max_uses_per_user=10,
        )

    def test_a_cart_the_old_flat_pre_check_would_have_rejected_now_succeeds(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        seated_org: Organization,
    ) -> None:
        """Premium + Standard = 110.00 real, but ``tier.price * 2`` = 100.00 — a false rejection.

        This is the case the struck "conservative pre-check" would also have failed:
        any estimate built from a price *lower* than the seats' actual prices
        underestimates the total and rejects a cart that genuinely qualifies.
        """
        code = self._code(seated_org, "105.00")

        tickets = _tickets(_buy(seated_event, offline_tier, member_user, seats[:2], code=code))

        assert [ticket.price_paid for ticket in tickets] == [Decimal("72.00"), Decimal("27.00")]

    def test_a_cart_the_old_flat_pre_check_would_have_accepted_is_now_rejected(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        sector: VenueSector,
        categories: tuple[PriceCategory, PriceCategory],
        seats: list[VenueSeat],
        member_user: RevelUser,
        seated_org: Organization,
    ) -> None:
        """Two Standard seats = 60.00 real, but ``tier.price * 2`` = 100.00 — a false acceptance.

        The buyer would have redeemed a "spend 100" code on a 60 euro cart.
        """
        _premium, standard = categories
        second_standard = VenueSeat.objects.create(
            sector=sector,
            label="A4",
            row_label="A",
            number=4,
            position={"x": 3, "y": 0},
            is_active=True,
            default_price_category=standard,
        )
        code = self._code(seated_org, "100.00")

        with pytest.raises(HttpError) as exc_info:
            _buy(seated_event, offline_tier, member_user, [seats[1], second_standard], code=code)

        assert exc_info.value.status_code == 400
        assert "Minimum purchase amount" in str(exc_info.value.message)
        assert not Ticket.objects.filter(event=seated_event, tier=offline_tier).exists()
        code.refresh_from_db()
        assert code.times_used == 0

    def test_the_threshold_is_the_pre_discount_total(
        self,
        seated_event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        seated_org: Organization,
    ) -> None:
        """160.00 gross clears a 160.00 minimum even though the buyer only pays 144.00.

        ``tier.price`` was always a list price, so comparing post-discount here would
        silently tighten every existing code.
        """
        code = self._code(seated_org, "160.00")

        tickets = _tickets(_buy(seated_event, offline_tier, member_user, seats, code=code))

        assert sum(t.price_paid for t in tickets if t.price_paid is not None) == Decimal("144.00")
