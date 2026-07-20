"""The single authority for "what does each ticket in this cart cost" (spec §5.1).

Pure module: it takes already-fetched objects and returns numbers. No queries, no
saves, no locking — the caller resolves the seats and holds the tier lock. That is
what makes it safe to call from inside ``create_batch``'s critical section, and
what lets it be tested without a database.

Money is ``Decimal`` end to end. Never ``float`` — binary floats cannot represent
money. Each ticket is rounded on its own and the rounded units are then summed;
downstream (platform fee, VAT) rounds the *total*, so round-then-sum vs
sum-then-round is a real cents-level difference and this module pins the former.
"""

import dataclasses
import typing as t
from decimal import Decimal
from uuid import UUID

import structlog

from events.service.discount_code_service import calculate_discounted_unit_price
from events.utils.tier_pricing import effective_category_price, parse_price_map

if t.TYPE_CHECKING:
    from events.models import DiscountCode, TicketTier, VenueSeat

logger = structlog.get_logger(__name__)

ZERO = Decimal("0.00")


@dataclasses.dataclass(frozen=True)
class TicketPrice:
    """What one ticket in the cart costs.

    Attributes:
        unit_price: Post-discount, pre-VAT price for this single ticket.
        discount_amount: What the code took off *this* ticket — not the tier-wide
            scalar. ``0.00`` when no code applies; callers stamping
            ``Ticket.discount_amount`` keep passing ``None`` when there is no
            discount code, matching today's column semantics.
    """

    unit_price: Decimal
    discount_amount: Decimal


@dataclasses.dataclass(frozen=True)
class BatchPricing:
    """The per-ticket price vector for a whole cart, in cart order.

    Attributes:
        lines: One entry per requested ticket, positionally aligned with the
            ``seats`` list handed to :func:`build_batch_pricing`.
        total: Sum of the ``unit_price`` values (post-discount, pre-VAT).
    """

    lines: list[TicketPrice]
    total: Decimal


def resolve_seat_price(
    tier: "TicketTier",
    seat: "VenueSeat | None",
    price_map: dict[UUID, Decimal],
) -> Decimal:
    """Resolve the pre-discount price of one seat (spec §4.3).

    Resolution order:

    - Seat painted with a category present in the map → the mapped price.
    - Seat painted with a category **absent** from the map → ``tier.price`` plus a
      structured warning. Write-time validation normally prevents this, but paint
      can change after a tier is saved; a config drift must never 500 a buyer.
    - Unpainted seat → ``tier.price``, no warning. This is the one legitimate,
      documented fallback.
    - No seat (general admission) or an empty map → ``tier.price``, no warning.

    Args:
        tier: The tier being purchased (already locked by the caller, if relevant).
        seat: The resolved seat, or ``None`` for general admission.
        price_map: Parsed ``{price_category_id: price}`` map for the tier.

    Returns:
        The pre-discount unit price for this seat.
    """
    if seat is None or not price_map:
        return tier.price

    category_id = seat.default_price_category_id
    if category_id is not None and category_id not in price_map:
        logger.warning(
            "seat_price_category_unpriced",
            tier_id=str(tier.pk),
            seat_id=str(seat.pk),
            price_category_id=str(category_id),
            fallback_price=str(tier.price),
        )
    return effective_category_price(price_map, category_id, tier.price)


def cart_is_certainly_free(
    tier: "TicketTier",
    *,
    pwyc_amount: Decimal | None = None,
    discount_code: "DiscountCode | None" = None,
) -> bool:
    """Could any ticket on this tier cost something, before the seats are known?

    Checkout needs this answer *before* it resolves seats, to skip work that a
    free cart doesn't need (the pre-lock VIES round-trip in ``create_batch``).
    Without seats the only safe answer is an **upper bound**: every price the
    tier can charge — its flat price and each category price — must discount to
    zero. Erring toward "not free" costs one avoidable network call; erring the
    other way would silently drop the buyer's VAT context.

    Args:
        tier: The tier being purchased.
        pwyc_amount: The buyer's pay-what-you-can amount, if any.
        discount_code: An already-validated discount code, if any.

    Returns:
        True when no ticket on this tier can cost anything.
    """
    if pwyc_amount is not None:
        return pwyc_amount <= ZERO
    if discount_code is None:
        return False
    candidates = [tier.price, *parse_price_map(tier.category_prices).values()]
    return all(calculate_discounted_unit_price(price, discount_code) <= ZERO for price in candidates)


def build_batch_pricing(
    tier: "TicketTier",
    seats: "t.Sequence[VenueSeat | None]",
    *,
    pwyc_amount: Decimal | None = None,
    discount_code: "DiscountCode | None" = None,
) -> BatchPricing:
    """Price every ticket in a cart.

    A pay-what-you-can amount wins outright and prices the whole cart uniformly;
    the category map is ignored (the two are mutually exclusive by tier
    validation, and discount codes are rejected on PWYC tiers upstream).
    Otherwise each seat gets its own base price from :func:`resolve_seat_price`
    and the discount is applied **per ticket** — so a €40 fixed-amount code on a
    €50 + €30 cart legitimately yields ``[10.00, 0.00]``.

    Args:
        tier: The tier being purchased.
        seats: Resolved seats in cart order; ``None`` entries are general admission.
        pwyc_amount: The buyer's chosen pay-what-you-can amount, if any.
        discount_code: An already-validated discount code, if any.

    Returns:
        The per-ticket vector and its total.
    """
    if pwyc_amount is not None:
        lines = [TicketPrice(unit_price=pwyc_amount, discount_amount=ZERO) for _ in seats]
        return BatchPricing(lines=lines, total=sum((line.unit_price for line in lines), ZERO))

    price_map = parse_price_map(tier.category_prices)
    lines = []
    for seat in seats:
        base_price = resolve_seat_price(tier, seat, price_map)
        if discount_code is None:
            lines.append(TicketPrice(unit_price=base_price, discount_amount=ZERO))
            continue
        unit_price = calculate_discounted_unit_price(base_price, discount_code)
        lines.append(TicketPrice(unit_price=unit_price, discount_amount=base_price - unit_price))

    return BatchPricing(lines=lines, total=sum((line.unit_price for line in lines), ZERO))
