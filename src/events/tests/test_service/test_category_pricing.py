"""Tests for ``events.service.seating.pricing`` — the per-seat pricing authority.

The module is pure (spec §5.1): it receives already-fetched objects and returns
numbers. These tests therefore build **unsaved** model instances and never touch
the database — if a query ever creeps into the module, this file stops working.

The centrepiece is :class:`TestFlatTierParity`: with an empty ``category_prices``
map, ``build_batch_pricing`` must reproduce today's arithmetic **exactly**. The
expected values are the ones today's pipeline produces:

- **no discount** — ``price_override`` is ``None``, so ``reserve_batch_payments``
  uses ``base_price = tier.price`` (``stripe_service.py:394``) for every
  ``Payment`` and ``total_amount = base_price * len(tickets)``; ``create_tickets``
  stamps ``discount_amount = None`` (``batch_ticket_service.py:701``).
- **discounted** — the controller pre-computes
  ``price_override = calculate_discounted_price(tier, dc)``
  (``event_public/tickets.py:178``, ``guest.py:314,439``) and that single scalar
  becomes every ticket's price; ``create_tickets`` computes
  ``discount_amount = calculate_discount_amount(tier, dc)`` once
  (``batch_ticket_service.py:705``) and stamps it on all of them (``:717``).
- **PWYC** — ``price_override`` is the buyer's amount, passed through untouched;
  discount codes are rejected on PWYC tiers (``discount_code_service.py:227``).

Each case is asserted twice: against a hand-computed ``Decimal`` literal, and
against the legacy tier-based helpers themselves (the executable oracle).
"""

import typing as t
import uuid
from decimal import Decimal

import pytest
from ninja.errors import HttpError

from events.models import DiscountCode, PriceCategory, TicketTier, VenueSeat
from events.service import discount_code_service
from events.service.seating import pricing
from events.utils.tier_pricing import parse_price_map

PREMIUM_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
STANDARD_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
UNPRICED_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")

LOGGER = "events.service.seating.pricing"


def make_tier(price: str = "50.00", category_prices: dict[str, str] | None = None) -> TicketTier:
    """Build an unsaved user-choice tier with the given flat price and map."""
    return TicketTier(
        id=uuid.uuid4(),
        name="Stalls",
        price=Decimal(price),
        category_prices=category_prices or {},
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
    )


def make_seat(category_id: uuid.UUID | None, category_name: str = "Painted") -> VenueSeat:
    """Build an unsaved seat, optionally painted with a price category.

    The category is attached as an unsaved instance (not just its id) so the FK is
    already cached — the refusal path reads ``.name`` for the error message and this
    file must never touch the database.
    """
    seat = VenueSeat(id=uuid.uuid4(), label="A1", row_label="A", number=1, adjacency_index=0)
    if category_id is None:
        seat.default_price_category = None
    else:
        seat.default_price_category = PriceCategory(id=category_id, name=category_name)
    return seat


def make_discount(kind: DiscountCode.DiscountType, value: str) -> DiscountCode:
    """Build an unsaved discount code of the given type and value."""
    return DiscountCode(
        code="TEST",
        discount_type=kind,
        discount_value=Decimal(value),
        currency="EUR",
    )


@pytest.fixture
def premium_map() -> dict[str, str]:
    """A two-category price map, as stored on the tier (decimal *strings*)."""
    return {str(PREMIUM_ID): "80.00", str(STANDARD_ID): "30.00"}


# ===========================================================================
# resolve_seat_price
# ===========================================================================


class TestResolveSeatPrice:
    """Spec §4.3 — the resolution order and its two very different fallbacks."""

    def test_painted_and_priced_seat_uses_the_map(self, premium_map: dict[str, str]) -> None:
        """A seat painted with a priced category is charged the mapped price."""
        tier = make_tier(category_prices=premium_map)
        price_map = parse_price_map(tier.category_prices)

        assert pricing.resolve_seat_price(tier, make_seat(PREMIUM_ID), price_map) == Decimal("80.00")
        assert pricing.resolve_seat_price(tier, make_seat(STANDARD_ID), price_map) == Decimal("30.00")

    def test_unpainted_seat_falls_back_to_tier_price_without_warning(
        self, premium_map: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one legitimate fallback: an unpainted seat is charged ``tier.price``, quietly."""
        tier = make_tier(category_prices=premium_map)
        price_map = parse_price_map(tier.category_prices)

        with caplog.at_level("WARNING", logger=LOGGER):
            resolved = pricing.resolve_seat_price(tier, make_seat(None), price_map)

        assert resolved == Decimal("50.00")
        assert caplog.records == []

    def test_painted_but_unpriced_seat_is_refused(
        self, premium_map: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Config drift (paint moved after the tier was saved) refuses the seat, naming the category.

        Falling back to ``tier.price`` here would charge the flat price for what the
        venue map calls a premium seat — the exact silent mispricing this feature
        exists to prevent. Paint is venue-scoped and deliberately never hard-fails,
        so the gap has to bite at checkout instead (decision 2026-07-20).
        """
        tier = make_tier(category_prices=premium_map)
        price_map = parse_price_map(tier.category_prices)
        seat = make_seat(UNPRICED_ID, category_name="Balcony")

        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(HttpError) as exc_info:
            pricing.resolve_seat_price(tier, seat, price_map)

        assert exc_info.value.status_code == 400
        assert "Balcony" in str(exc_info.value.message)
        assert any("seat_price_category_unpriced" in record.message for record in caplog.records)
        assert any(str(UNPRICED_ID) in record.message for record in caplog.records)

    def test_general_admission_has_no_seat(self, premium_map: dict[str, str]) -> None:
        """``seat is None`` (GA) resolves to the flat price with no warning."""
        tier = make_tier(category_prices=premium_map)
        price_map = parse_price_map(tier.category_prices)

        assert pricing.resolve_seat_price(tier, None, price_map) == Decimal("50.00")

    def test_empty_map_ignores_the_paint(self, caplog: pytest.LogCaptureFixture) -> None:
        """A flat tier charges ``tier.price`` even for painted seats, and stays silent."""
        tier = make_tier()

        with caplog.at_level("WARNING", logger=LOGGER):
            resolved = pricing.resolve_seat_price(tier, make_seat(PREMIUM_ID), {})

        assert resolved == Decimal("50.00")
        assert caplog.records == []


# ===========================================================================
# THE PARITY TEST — a flat tier must behave byte-identically to today
# ===========================================================================


class TestFlatTierParity:
    """An empty map must reproduce today's arithmetic exactly (plan Task 2).

    This is what makes ripping out the uniform scalar (Tasks 5-7) safe. Exact
    ``Decimal`` equality only — no float approximation anywhere.
    """

    @pytest.mark.parametrize("count", [1, 3])
    def test_flat_tier_no_discount(self, count: int) -> None:
        """Today: ``price_override=None`` → every Payment is ``tier.price``."""
        tier = make_tier("50.00")
        seats: list[VenueSeat | None] = [make_seat(None) for _ in range(count)]

        result = pricing.build_batch_pricing(tier, seats)

        assert [line.unit_price for line in result.lines] == [Decimal("50.00")] * count
        assert [line.discount_amount for line in result.lines] == [Decimal("0.00")] * count
        assert result.total == Decimal("50.00") * count

    @pytest.mark.parametrize("count", [1, 3])
    def test_flat_tier_percentage_discount(self, count: int) -> None:
        """Today: ``price_override = calculate_discounted_price(tier, dc)``, uniform."""
        tier = make_tier("50.00")
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "20.00")
        seats: list[VenueSeat | None] = [make_seat(None) for _ in range(count)]

        result = pricing.build_batch_pricing(tier, seats, discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("40.00")] * count
        assert [line.discount_amount for line in result.lines] == [Decimal("10.00")] * count
        assert result.total == Decimal("40.00") * count
        # …and against the legacy helpers, which are the executable baseline.
        legacy_price = discount_code_service.calculate_discounted_price(tier, dc)
        legacy_amount = discount_code_service.calculate_discount_amount(tier, dc)
        assert all(line.unit_price == legacy_price for line in result.lines)
        assert all(line.discount_amount == legacy_amount for line in result.lines)
        assert result.total == legacy_price * count

    @pytest.mark.parametrize("count", [1, 3])
    def test_flat_tier_fixed_amount_discount(self, count: int) -> None:
        """Today: a FIXED_AMOUNT code is also delivered as the uniform scalar."""
        tier = make_tier("50.00")
        dc = make_discount(DiscountCode.DiscountType.FIXED_AMOUNT, "12.50")
        seats: list[VenueSeat | None] = [make_seat(None) for _ in range(count)]

        result = pricing.build_batch_pricing(tier, seats, discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("37.50")] * count
        assert [line.discount_amount for line in result.lines] == [Decimal("12.50")] * count
        assert result.total == Decimal("37.50") * count
        legacy_price = discount_code_service.calculate_discounted_price(tier, dc)
        legacy_amount = discount_code_service.calculate_discount_amount(tier, dc)
        assert all(line.unit_price == legacy_price for line in result.lines)
        assert all(line.discount_amount == legacy_amount for line in result.lines)

    @pytest.mark.parametrize("count", [1, 3])
    def test_pwyc_no_discount(self, count: int) -> None:
        """Today: the buyer's PWYC amount is passed through untouched, uniform."""
        tier = make_tier("0.00")
        tier.price_type = TicketTier.PriceType.PWYC
        seats: list[VenueSeat | None] = [make_seat(None) for _ in range(count)]

        result = pricing.build_batch_pricing(tier, seats, pwyc_amount=Decimal("17.30"))

        assert [line.unit_price for line in result.lines] == [Decimal("17.30")] * count
        assert [line.discount_amount for line in result.lines] == [Decimal("0.00")] * count
        assert result.total == Decimal("17.30") * count

    @pytest.mark.parametrize(
        "kind,value",
        [
            (DiscountCode.DiscountType.PERCENTAGE, "20.00"),
            (DiscountCode.DiscountType.FIXED_AMOUNT, "12.50"),
        ],
    )
    @pytest.mark.parametrize("count", [1, 3])
    def test_pwyc_ignores_a_discount_code(self, kind: DiscountCode.DiscountType, value: str, count: int) -> None:
        """PWYC + discount is rejected upstream; if it ever arrives, PWYC wins.

        ``_validate_core`` refuses discount codes on PWYC tiers
        (``discount_code_service.py:227``), so this combination is unreachable in
        production. Pinning "PWYC wins, silently" keeps the pipeline from
        inventing a third answer if that guard is ever relaxed.
        """
        tier = make_tier("0.00")
        tier.price_type = TicketTier.PriceType.PWYC
        dc = make_discount(kind, value)
        seats: list[VenueSeat | None] = [make_seat(None) for _ in range(count)]

        result = pricing.build_batch_pricing(tier, seats, pwyc_amount=Decimal("17.30"), discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("17.30")] * count
        assert result.total == Decimal("17.30") * count

    def test_parity_holds_for_a_rounding_heavy_percentage(self) -> None:
        """33.33% off 50.00 rounds to 33.34 today (half-even on 33.335) — keep it."""
        tier = make_tier("50.00")
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "33.33")

        result = pricing.build_batch_pricing(tier, [None, None], discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("33.34"), Decimal("33.34")]
        assert [line.discount_amount for line in result.lines] == [Decimal("16.66"), Decimal("16.66")]
        assert result.total == Decimal("66.68")
        assert result.total == discount_code_service.calculate_discounted_price(tier, dc) * 2

    def test_empty_cart_totals_zero(self) -> None:
        """A zero-length cart is arithmetically well-defined."""
        result = pricing.build_batch_pricing(make_tier(), [])

        assert result.lines == []
        assert result.total == Decimal("0.00")


# ===========================================================================
# Category pricing — the new behaviour
# ===========================================================================


class TestCategoryPricing:
    """Mixed carts, per-ticket discounts, and the rounding contract."""

    def test_mixed_cart_prices_each_seat_from_its_category(self, premium_map: dict[str, str]) -> None:
        """Premium 80 + Standard 30 + unpainted 50 = 160.00, in cart order."""
        tier = make_tier("50.00", premium_map)
        seats: list[VenueSeat | None] = [make_seat(PREMIUM_ID), make_seat(STANDARD_ID), make_seat(None)]

        result = pricing.build_batch_pricing(tier, seats)

        assert [line.unit_price for line in result.lines] == [Decimal("80.00"), Decimal("30.00"), Decimal("50.00")]
        assert result.total == Decimal("160.00")

    def test_percentage_discount_is_applied_per_ticket(self, premium_map: dict[str, str]) -> None:
        """Spec §5.3: the true per-ticket discounts are 8.00 and 3.00, not 5.00 twice."""
        tier = make_tier("50.00", premium_map)
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "10.00")
        seats: list[VenueSeat | None] = [make_seat(PREMIUM_ID), make_seat(STANDARD_ID)]

        result = pricing.build_batch_pricing(tier, seats, discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("72.00"), Decimal("27.00")]
        assert [line.discount_amount for line in result.lines] == [Decimal("8.00"), Decimal("3.00")]
        assert result.total == Decimal("99.00")
        # The scalar this replaces would have charged 45.00 per ticket.
        assert result.total != discount_code_service.calculate_discounted_price(tier, dc) * 2

    def test_fixed_amount_discount_floors_each_ticket_at_zero(self) -> None:
        """A €40 code on a €50 + €30 cart yields ``[10.00, 0.00]`` — a legal cart."""
        tier = make_tier("50.00", {str(PREMIUM_ID): "50.00", str(STANDARD_ID): "30.00"})
        dc = make_discount(DiscountCode.DiscountType.FIXED_AMOUNT, "40.00")
        seats: list[VenueSeat | None] = [make_seat(PREMIUM_ID), make_seat(STANDARD_ID)]

        result = pricing.build_batch_pricing(tier, seats, discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("10.00"), Decimal("0.00")]
        # The discount never exceeds what the ticket cost: 40.00 then 30.00, not 40.00 twice.
        assert [line.discount_amount for line in result.lines] == [Decimal("40.00"), Decimal("30.00")]
        assert result.total == Decimal("10.00")

    def test_unpriced_category_in_a_batch_refuses_the_whole_cart(
        self, premium_map: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """One unpriced seat in the cart refuses that cart — it is never quietly flat-priced.

        The refusal is seat-scoped in the sense that seats in priced categories stay
        sellable; the *buyer* just has to drop the offending one from this cart.
        """
        tier = make_tier("50.00", premium_map)
        seats: list[VenueSeat | None] = [make_seat(PREMIUM_ID), make_seat(UNPRICED_ID, category_name="Balcony")]

        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(HttpError) as exc_info:
            pricing.build_batch_pricing(tier, seats)

        assert exc_info.value.status_code == 400
        assert "Balcony" in str(exc_info.value.message)
        assert sum("seat_price_category_unpriced" in record.message for record in caplog.records) == 1

    def test_rounds_per_ticket_then_sums(self) -> None:
        """Round-then-sum is the contract; sum-then-round differs by a cent here.

        3 × 10.00 at 33.33% off → 6.667 per ticket → 6.67 each → **20.01**.
        Summing first (30.00 → 20.001 → 20.00) would be a cent short, and the
        platform fee later rounds ``ROUND_HALF_UP`` on this total.
        """
        tier = make_tier("10.00")
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "33.33")

        result = pricing.build_batch_pricing(tier, [None, None, None], discount_code=dc)

        assert [line.unit_price for line in result.lines] == [Decimal("6.67")] * 3
        assert result.total == Decimal("20.01")
        naive_sum_then_round = (tier.price * 3 * (Decimal("100") - dc.discount_value) / Decimal("100")).quantize(
            Decimal("0.01")
        )
        assert naive_sum_then_round == Decimal("20.00")
        assert result.total != naive_sum_then_round

    def test_lines_are_positionally_aligned_with_seats(self, premium_map: dict[str, str]) -> None:
        """Cart order is load-bearing — Payments and Stripe line items zip on it."""
        tier = make_tier("50.00", premium_map)
        seats: list[VenueSeat | None] = [make_seat(STANDARD_ID), make_seat(PREMIUM_ID), make_seat(STANDARD_ID)]

        result = pricing.build_batch_pricing(tier, seats)

        assert len(result.lines) == len(seats)
        assert [line.unit_price for line in result.lines] == [Decimal("30.00"), Decimal("80.00"), Decimal("30.00")]

    def test_results_are_immutable(self) -> None:
        """Frozen dataclasses — nothing downstream can rewrite a resolved price."""
        result = pricing.build_batch_pricing(make_tier(), [None])

        with pytest.raises(Exception):
            result.lines[0].unit_price = Decimal("0.01")  # type: ignore[misc]

    def test_never_produces_a_float(self, premium_map: dict[str, str]) -> None:
        """Money is ``Decimal`` end to end."""
        tier = make_tier("50.00", premium_map)
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "33.33")
        seats: list[VenueSeat | None] = [make_seat(PREMIUM_ID), make_seat(None)]

        result = pricing.build_batch_pricing(tier, seats, discount_code=dc)

        values: list[t.Any] = [result.total]
        for line in result.lines:
            values += [line.unit_price, line.discount_amount]
        assert all(isinstance(value, Decimal) for value in values)


class TestCartIsCertainlyFree:
    """The seat-blind upper bound checkout uses to skip pre-lock work (plan Task 5)."""

    def test_pwyc_zero_is_free_and_pwyc_positive_is_not(self) -> None:
        """PWYC prices the whole cart uniformly, so the amount alone decides."""
        tier = make_tier("50.00")

        assert pricing.cart_is_certainly_free(tier, pwyc_amount=Decimal("0.00")) is True
        assert pricing.cart_is_certainly_free(tier, pwyc_amount=Decimal("0.01")) is False

    def test_no_discount_is_never_certainly_free(self) -> None:
        """Without buyer input the tier price stands — even a 0.00 tier stays on the paid path."""
        assert pricing.cart_is_certainly_free(make_tier("0.00")) is False

    def test_code_covering_the_flat_price_is_free(self) -> None:
        """A FIXED_AMOUNT code worth the whole ticket zeroes a flat tier."""
        tier = make_tier("25.00")
        dc = make_discount(DiscountCode.DiscountType.FIXED_AMOUNT, "25.00")

        assert pricing.cart_is_certainly_free(tier, discount_code=dc) is True

    def test_partial_code_is_not_free(self) -> None:
        """A code that leaves anything on any price is not certainly free."""
        tier = make_tier("25.00")
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "99.00")

        assert pricing.cart_is_certainly_free(tier, discount_code=dc) is False

    def test_a_dearer_category_keeps_the_cart_payable(self, premium_map: dict[str, str]) -> None:
        """The bound is over EVERY price the tier can charge, not just ``tier.price``.

        A code covering the flat 50.00 leaves 30.00 on a Premium seat, so the
        buyer's VAT context must still be resolved.
        """
        tier = make_tier("50.00", premium_map)
        dc = make_discount(DiscountCode.DiscountType.FIXED_AMOUNT, "50.00")

        assert pricing.cart_is_certainly_free(tier, discount_code=dc) is False

    def test_code_covering_every_category_is_free(self, premium_map: dict[str, str]) -> None:
        """When the code covers the dearest category too, nothing can be charged."""
        tier = make_tier("50.00", premium_map)
        dc = make_discount(DiscountCode.DiscountType.PERCENTAGE, "100.00")

        assert pricing.cart_is_certainly_free(tier, discount_code=dc) is True


class TestShouldStampPricePaid:
    """Spec §5.5 — the one write-side authority, over every combination it distinguishes.

    NULL ``price_paid`` is a positive claim that ``tier.price`` reconstructs the amount,
    so False is the answer that carries risk: it is only correct for a plain purchase on
    a flat tier. Each row below is one way that claim can become false.
    """

    @pytest.mark.parametrize(
        ("category_prices", "pwyc_amount", "has_discount", "is_comp", "expected"),
        [
            pytest.param(None, None, False, False, False, id="flat-plain-purchase-stays-null"),
            pytest.param(None, Decimal("0.00"), False, False, True, id="pwyc-zero-still-stamps"),
            pytest.param(None, Decimal("12.50"), False, False, True, id="pwyc-amount"),
            pytest.param(None, None, True, False, True, id="discount-code"),
            pytest.param(None, None, False, True, True, id="comp"),
            pytest.param({}, None, False, False, False, id="empty-map-is-a-flat-tier"),
            pytest.param("MAP", None, False, False, True, id="category-priced-tier"),
            pytest.param("MAP", Decimal("5.00"), False, False, True, id="category-priced-and-pwyc"),
            pytest.param("MAP", None, True, False, True, id="category-priced-and-discount"),
            pytest.param("MAP", None, False, True, True, id="category-priced-comp"),
        ],
    )
    def test_decision_table(
        self,
        category_prices: dict[str, str] | str | None,
        pwyc_amount: Decimal | None,
        has_discount: bool,
        is_comp: bool,
        expected: bool,
        premium_map: dict[str, str],
    ) -> None:
        """Every distinguished combination, asserted against the contract."""
        raw = premium_map if category_prices == "MAP" else t.cast("dict[str, str] | None", category_prices)
        tier = make_tier("50.00", raw)

        assert (
            pricing.should_stamp_price_paid(tier, pwyc_amount=pwyc_amount, has_discount=has_discount, is_comp=is_comp)
            is expected
        )

    def test_defaults_are_the_plain_purchase(self, premium_map: dict[str, str]) -> None:
        """With no purchase context at all only the tier's map can force a stamp."""
        assert pricing.should_stamp_price_paid(make_tier("50.00")) is False
        assert pricing.should_stamp_price_paid(make_tier("50.00", premium_map)) is True


class TestPricePaidIsAdminEntered:
    """Spec §5.5 — the mutation-side mirror: who may write *or clear* ``price_paid``."""

    @pytest.mark.parametrize(
        ("price_type", "expected"),
        [
            pytest.param(TicketTier.PriceType.PWYC, True, id="pwyc-is-admin-entered"),
            pytest.param(TicketTier.PriceType.FIXED, False, id="fixed-is-resolved"),
        ],
    )
    def test_only_pwyc_is_admin_entered(self, price_type: TicketTier.PriceType, expected: bool) -> None:
        """The predicate is exactly "PWYC" — nothing else may be typed over or cleared."""
        tier = make_tier("50.00")
        tier.price_type = price_type

        assert pricing.price_paid_is_admin_entered(tier) is expected

    def test_a_category_priced_tier_is_never_admin_entered(self, premium_map: dict[str, str]) -> None:
        """The bug this predicate exists to prevent: unconfirm must not clear a resolved price."""
        tier = make_tier("50.00", premium_map)

        assert pricing.should_stamp_price_paid(tier) is True
        assert pricing.price_paid_is_admin_entered(tier) is False


def test_module_is_pure_and_needs_no_database(premium_map: dict[str, str]) -> None:
    """No ``django_db`` marker anywhere in this file — a query here would error out."""
    tier = make_tier("50.00", premium_map)

    result = pricing.build_batch_pricing(tier, [make_seat(PREMIUM_ID)])

    assert result.total == Decimal("80.00")
