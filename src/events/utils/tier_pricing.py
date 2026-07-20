"""Parsing and write-time validation for ``TicketTier.category_prices``.

The map is ``{str(PriceCategory.id): decimal-string}``. Money is always a
``Decimal`` parsed from a string — never a float, which cannot represent money.

This module is pure utility: it touches models only, never services, so it is
safe to import from ``events.models``.
"""

import typing as t
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError

if t.TYPE_CHECKING:
    from events.models import TicketTier

FIELD = "category_prices"
ONLINE_MINIMUM = Decimal("1")


def _fail(message: str) -> t.NoReturn:
    raise DjangoValidationError({FIELD: message})


def parse_price_map(raw: t.Any) -> dict[UUID, Decimal]:
    """Parse the stored JSON map into ``{category_id: price}``.

    Args:
        raw: The raw field value, expected to be a mapping of UUID strings to
            decimal strings.

    Returns:
        The parsed map. An empty/blank value yields an empty dict.

    Raises:
        DjangoValidationError: If the container, a key, or a value is malformed,
            or a price is negative.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        _fail("Category prices must be a mapping of price category id to price.")

    parsed: dict[UUID, Decimal] = {}
    for key, value in raw.items():
        try:
            category_id = UUID(str(key))
        except ValueError, AttributeError, TypeError:
            _fail(f"'{key}' is not a valid price category id.")
        if isinstance(value, (float, bool)) or value is None:
            _fail(f"Price for category {category_id} must be a decimal string.")
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            _fail(f"'{value}' is not a valid price for category {category_id}.")
        if not price.is_finite() or price < 0:
            _fail(f"Price for category {category_id} must be a non-negative number.")
        parsed[category_id] = price
    return parsed


def validate_category_prices(tier: "TicketTier") -> None:
    """Validate a tier's category price map (spec §4.2 and §4.3).

    A non-empty map requires a ``user_choice`` tier whose categories all belong to
    the tier's venue, is mutually exclusive with PWYC, respects the ONLINE price
    floor, and must price every category painted on an active seat of the tier's
    sector.

    Args:
        tier: The tier being cleaned. ``venue_id``/``sector_id`` are expected to
            be resolved already.

    Raises:
        DjangoValidationError: If any rule is violated.
    """
    from events.models import PriceCategory, VenueSeat

    prices = parse_price_map(tier.category_prices)
    if not prices:
        return

    if tier.seat_assignment_mode != tier.SeatAssignmentMode.USER_CHOICE:
        _fail("Category prices are only supported for user-choice tiers. Clear them to change the seating mode.")
    if tier.price_type == tier.PriceType.PWYC:
        _fail("A tier is either pay-what-you-can or category-priced, never both.")
    if tier.payment_method == tier.PaymentMethod.ONLINE:
        low = sorted(str(cid) for cid, price in prices.items() if price < ONLINE_MINIMUM)
        if low:
            _fail(f"Online tiers require every category price to be at least 1: {', '.join(low)}.")

    known = set(
        PriceCategory.objects.filter(venue_id=tier.venue_id, id__in=prices).values_list("id", flat=True)
        if tier.venue_id
        else []
    )
    unknown = sorted(str(cid) for cid in prices if cid not in known)
    if unknown:
        _fail(f"These price categories do not belong to the tier's venue: {', '.join(unknown)}.")

    painted = set(
        VenueSeat.objects.filter(sector_id=tier.sector_id, is_active=True, default_price_category__isnull=False)
        .values_list("default_price_category_id", flat=True)
        .distinct()
    )
    missing = painted - prices.keys()
    if missing:
        names = sorted(PriceCategory.objects.filter(id__in=missing).values_list("name", flat=True))
        _fail(f"Every painted category in the sector must be priced. Missing: {', '.join(names)}.")
