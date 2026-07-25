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
from django.utils.translation import gettext as _
from ninja.errors import HttpError

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

    @property
    def gross_total(self) -> Decimal:
        """The cart's **pre-discount** total.

        What a discount code's ``min_purchase_amount`` threshold compares against
        (spec §5.6): ``tier.price`` was always a list price, so measuring the
        threshold post-discount would silently tighten every existing code.
        """
        return sum((line.unit_price + line.discount_amount for line in self.lines), ZERO)


def resolve_seat_price(
    tier: "TicketTier",
    seat: "VenueSeat | None",
    price_map: dict[UUID, Decimal],
) -> Decimal:
    """Resolve the pre-discount price of one seat (spec §4.3).

    The single price authority, shared by every seat mode: user-choice carts, the
    best-available picker's assigned seats, the VAT preview and the box office all
    land here, so a quote can never be computed differently from the charge.

    Resolution order:

    - Seat painted with a category present in the map → the mapped price.
    - Seat painted with a category **absent** from the map → **refuse the sale**.
    - Unpainted seat → ``tier.price``, no warning. This is the one legitimate,
      documented fallback.
    - No seat (general admission) or an empty map → ``tier.price``, no warning.

    Why refusing beats falling back (decision 2026-07-20): the map's keys *are* the
    set of categories a tier sells, and a partial map is legal in both directions.
    On a **best-available** tier it is the normal, intended shape — the map's keys
    are the tier's sellable zones, and a category left out is simply not in this
    tier's pool. On a **user-choice** tier it can instead be an unfinished
    configuration: ``paint_seats`` is venue-scoped, so it deliberately does **not**
    hard-fail when it leaves a tier under-covered — one event's pricing config must
    not block routine map work for every other event at that venue. Either way the
    answer at the till is the same, and it is not "fall back to ``tier.price``":
    charging the flat price for a premium seat is exactly the silent mispricing this
    feature exists to prevent. Only the affected seats are refused — seats in priced
    categories still sell normally.

    Args:
        tier: The tier being purchased (already locked by the caller, if relevant).
        seat: The resolved seat, or ``None`` for general admission.
        price_map: Parsed ``{price_category_id: price}`` map for the tier.

    Returns:
        The pre-discount unit price for this seat.

    Raises:
        HttpError: 400, when the seat is painted into a category the tier does not
            price. The message names both the seat and the category: door staff read
            it off a box-office refusal and need to know which seat to stop selling,
            and the organizer reading the support ticket needs to know which category
            to add. It states the fact ("this tier does not sell that category")
            rather than diagnosing a cause — one wording, because the buyer's and the
            door's next move is identical whether the gap is deliberate or an
            oversight, and only the organizer can tell the two apart.
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
        )
        # Lazy FK load on the error path only — one query, never on the happy path.
        category_name = getattr(seat.default_price_category, "name", str(category_id))
        raise HttpError(
            400,
            str(_('Seat {seat} is in the "{category}" price category, which this ticket tier does not sell.')).format(
                seat=seat.label, category=category_name
            ),
        )
    return effective_category_price(price_map, category_id, tier.price)


def recorded_or_resolved_price(
    tier: "TicketTier",
    seat: "VenueSeat | None",
    price_paid: Decimal | None,
) -> Decimal:
    """What an already-sold ticket actually cost, for the money-bearing read paths (spec §5.5).

    ``Ticket.price_paid`` is purchase-time truth and always wins. When it is NULL the
    historical fallback was ``tier.price``, which is only correct while the tier charges
    one price. On a category-priced tier that silently under- or over-states every
    non-flat seat — capping refunds at the wrong number and mis-reporting revenue — so
    the seat's own category price is resolved instead.

    **Never raises** — deliberately *not* routed through :func:`resolve_seat_price`, which
    refuses an unpriced category at checkout. This is a read path for money that has already
    changed hands; refusing here would break refunding a ticket whose category was unpriced
    after the sale. The ``price_paid`` invariant is also time-scoped: tickets sold *before*
    a tier opted into category pricing legitimately carry NULL (``ticket.py:674-681``),
    and refunding them must keep working. A NULL on a category-priced tier is logged as
    an anomaly, priced from the seat (flat price if its category is unpriced), and
    allowed through.

    **Online tickets are the other legitimate NULL — permanently** (#758, see
    :func:`should_stamp_price_paid`): their 1:1 ``Payment`` row is authoritative, and
    its amount can be *net* (reverse charge), which no tier or seat price reconstructs.
    Callers that can see online rows must consult ``ticket.payment`` before falling
    back here (as ``ApplePassGenerator._resolve_price`` does); the paths that call this
    directly (offline refunds, the revenue report's offline rows) never carry online
    tickets.

    Callers pass ``ticket.tier``/``ticket.seat`` — pre-fetch both (``select_related``)
    when calling this in a loop.

    Args:
        tier: The ticket's tier.
        seat: The ticket's seat, or ``None`` for general admission.
        price_paid: The recorded amount, if any.

    Returns:
        The amount this ticket is treated as having cost.
    """
    if price_paid is not None:
        return price_paid
    price_map = parse_price_map(tier.category_prices)
    if not price_map:
        return tier.price
    logger.warning(
        "ticket_price_paid_missing_on_category_priced_tier",
        tier_id=str(tier.pk),
        seat_id=str(seat.pk) if seat is not None else None,
    )
    category_id = seat.default_price_category_id if seat is not None else None
    return effective_category_price(price_map, category_id, tier.price)


def should_stamp_price_paid(
    tier: "TicketTier",
    *,
    pwyc_amount: Decimal | None = None,
    has_discount: bool = False,
    is_comp: bool = False,
) -> bool:
    """Does this sale have to record its own ``price_paid``? (spec §5.5).

    The write-side counterpart of :func:`recorded_or_resolved_price`, and the single
    authority every ticket-creating path asks. A NULL ``price_paid`` is not "unknown" —
    it is a positive claim that ``tier.price`` still reconstructs the amount, which both
    the revenue report and the refund ceiling lean on. So stamp exactly when that claim
    is false:

    - **PWYC** — the buyer chose the amount; ``tier.price`` is not it.
    - **Discount code** — the amount is per ticket, and a mixed cart differs row to row.
    - **Comp** — a giveaway is ``0.00``, whatever the seat is worth.
    - **Category-priced tier** — the seat's price, not the tier's flat one, and it must
      survive a later repricing.

    Otherwise leave it NULL: a plain purchase on a flat tier (``tier.price`` stands) and
    every online row. The online carve-out is **permanent** (decision on #758, option a):
    the 1:1 ``Payment`` row is authoritative there, and ``Payment.amount`` is the amount
    actually charged — *net* for a reverse-charge buyer — so copying it into
    ``price_paid`` would make the column's meaning depend on the buyer's VAT status
    (and two "price paid" numbers on one row is worse than none). ``_online_checkout``
    therefore never asks this question and never stamps, even when this function would
    say True (e.g. a category-priced tier); every money-bearing reader of an online
    ticket consults its ``Payment`` row instead (the revenue report aggregates the
    payments directly; the wallet pass checks ``ticket.payment`` before falling back).
    Pinned in ``test_online_checkout_price_paid.py`` — do not "fix" a NULL online row
    by stamping. The zeroed-ONLINE reroute to free checkout is not part of the
    carve-out: it has no ``Payment`` row, so it stamps like any free sale.

    The invariant is **time-scoped** — tickets sold before a tier opted into category
    pricing legitimately carry NULL — so it can never be a DB constraint or a raise, and
    a shared function is the only enforcement available. Amend the rule *here* when a new
    ticket-writing path lands.

    Not covered: series-pass tickets (``series_pass_service.materialize_tickets``,
    ``series_pass_purchase``), whose ``price_paid`` is a share of the pass price. No tier
    can reconstruct that, so they always stamp and have no decision to make.

    Args:
        tier: The tier being sold (the *locked* tier wherever one is held).
        pwyc_amount: The buyer's pay-what-you-can amount, if any.
        has_discount: Whether a discount code applies to this purchase.
        is_comp: Whether this is a giveaway priced ``0.00`` regardless of the tier.

    Returns:
        True when the created ticket must carry an explicit ``price_paid``.
    """
    # Truthiness, not parse_price_map: identical on every value the field can legally
    # hold, and this must not start raising on a legacy malformed map at checkout time.
    return pwyc_amount is not None or has_discount or is_comp or bool(tier.category_prices)


def price_paid_is_admin_entered(tier: "TicketTier") -> bool:
    """Is this tier's ``price_paid`` an admin's input rather than a resolved fact? (spec §5.5).

    The mutation-side reading of :func:`should_stamp_price_paid`, asked by the flows that
    write or clear ``price_paid`` on an *existing* ticket — confirm, unconfirm, check-in.

    On a PWYC tier the amount is typed in by staff when they take the money, so those flows
    own it: they may require it, overwrite it, and — on unconfirm — clear it, because
    confirming again asks for it afresh. On every other tier a non-null ``price_paid`` is a
    *resolved* fact of the sale (a category price, a per-ticket discount, a comp) that
    nothing downstream can rebuild from ``tier.price``; clearing it mis-reports revenue and
    caps refunds at the wrong number, and ``confirm_ticket_payment`` refuses to accept a
    price for a non-PWYC tier, so nothing could ever put it back. That gap was a real bug.

    Callers may narrow further (check-in also excludes pass tickets and online payment
    methods) but must never widen.

    Args:
        tier: The ticket's tier.

    Returns:
        True when confirm/unconfirm/check-in may write or clear ``price_paid``.
    """
    return bool(tier.price_type == tier.PriceType.PWYC)


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

    Raises:
        HttpError: 400, when any seat is painted into a category the tier does not
            price — see :func:`resolve_seat_price`.
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
