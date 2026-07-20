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
    from django.db.models import QuerySet

    from events.models import PriceCategory, TicketTier

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


def effective_category_price(price_map: dict[UUID, Decimal], category_id: UUID | None, flat_price: Decimal) -> Decimal:
    """Resolve what one price category costs on a tier (spec §4.3).

    The single authority for the category → price fallback chain, shared by the
    checkout resolver (:func:`events.service.seating.pricing.resolve_seat_price`)
    and by the buyer-facing tier payload. They must never drift: a displayed price
    that disagrees with the charged price is worse than no price at all.

    Args:
        price_map: The tier's parsed ``{category_id: price}`` map.
        category_id: The seat's painted category, or ``None`` for an unpainted seat.
        flat_price: The tier's flat ``price``, used as the fallback.

    Returns:
        The pre-discount price for a seat in that category.
    """
    if category_id is None:
        return flat_price
    return price_map.get(category_id, flat_price)


def painted_categories(sector_id: t.Any) -> "QuerySet[PriceCategory]":
    """The price categories painted on at least one active seat of a sector.

    The single definition of "painted", shared by write-time validation and by the
    read paths that surface a tier's pricing gaps. A ``None`` sector yields an empty
    queryset (an unseated tier has nothing painted).

    Args:
        sector_id: The sector to inspect.

    Returns:
        A distinct queryset of the categories in use, unordered.
    """
    from events.models import PriceCategory

    if sector_id is None:
        return PriceCategory.objects.none()
    return PriceCategory.objects.filter(seats__sector_id=sector_id, seats__is_active=True).distinct()


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
    from events.models import PriceCategory

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
    unknown = {cid for cid in prices if cid not in known}
    if unknown:
        # Name whatever resolves — a category from another venue still has a name the admin
        # recognises, and a bare UUID is unrenderable in the tier form. Ids that match nothing
        # at all fall back to the raw value.
        elsewhere = dict(PriceCategory.objects.filter(id__in=unknown).values_list("id", "name"))
        labels = sorted(elsewhere.get(cid, str(cid)) for cid in unknown)
        _fail(f"These price categories do not belong to the tier's venue: {', '.join(labels)}.")

    painted = set(painted_categories(tier.sector_id).values_list("id", flat=True))
    missing = painted - prices.keys()
    if missing:
        names = sorted(PriceCategory.objects.filter(id__in=missing).values_list("name", flat=True))
        _fail(f"Every painted category in the sector must be priced. Missing: {', '.join(names)}.")
