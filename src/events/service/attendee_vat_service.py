"""Attendee VAT calculation for checkout and invoice generation.

Determines VAT treatment based on buyer billing info (country, VAT ID)
and the seller (organization) VAT configuration. The tier price is always
VAT-inclusive; for reverse charge / non-EU buyers, the price is reduced
to the net amount.

EU VAT rules for event tickets:
- Domestic (same country): seller's VAT rate applies regardless of B2B/B2C
- EU cross-border B2B (valid VAT ID, different country): reverse charge (0%)
- EU cross-border B2C (no valid VAT ID): seller's VAT rate applies
- Non-EU buyer: no VAT (export of services)
"""

import typing as t
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from django.utils.translation import gettext as _

from common.constants import EU_MEMBER_STATES
from common.service.vat_utils import TWO_PLACES, calculate_vat_inclusive

if t.TYPE_CHECKING:
    from events.models.event import Event
    from events.models.organization import Organization
    from events.models.ticket import TicketTier
    from events.models.venue import VenueSeat
    from events.schema.ticket import BuyerBillingInfoSchema, VATPreviewItemSchema
    from events.service.seating.pricing import BatchPricing


@dataclass(frozen=True)
class AttendeeVATResult:
    """Per-ticket VAT breakdown after applying buyer-specific rules."""

    effective_price: Decimal  # What the buyer actually pays (may differ from tier gross)
    net_amount: Decimal
    vat_amount: Decimal
    vat_rate: Decimal
    reverse_charge: bool


@dataclass(frozen=True)
class BuyerVATContext:
    """The network half of attendee VAT resolution, price-independent (#632).

    Captures the VIES validation + buyer-country derivation done before the
    TicketTier lock, so the price arithmetic (determine_attendee_vat) can be
    re-run under the lock against the fresh locked price — an organizer
    repricing the tier during the VIES round-trip can't strand a stale amount.
    """

    buyer_country: str | None
    buyer_vat_validated: bool


def _normalize_country(code: str) -> str:
    """Normalize country codes for VAT comparison.

    Greece uses "EL" as the VIES/VAT prefix but "GR" as the ISO 3166-1 code.
    Normalize to ISO to avoid misclassifying Greek domestic sales as cross-border.
    """
    code = code.upper()
    if code == "EL":
        return "GR"
    return code


def determine_attendee_vat(
    gross_price: Decimal,
    seller_vat_rate: Decimal,
    seller_country: str,
    buyer_country: str,
    buyer_vat_id_valid: bool,
) -> AttendeeVATResult:
    """Determine VAT treatment for an attendee ticket purchase.

    Args:
        gross_price: The VAT-inclusive tier price.
        seller_vat_rate: The seller's (org) applicable VAT rate.
        seller_country: The seller's VAT country code.
        buyer_country: The buyer's billing country code.
        buyer_vat_id_valid: Whether the buyer has a validated VAT ID.

    Returns:
        AttendeeVATResult with effective price and VAT breakdown.
    """
    seller_country = _normalize_country(seller_country)
    buyer_country = _normalize_country(buyer_country)
    buyer_in_eu = buyer_country in EU_MEMBER_STATES
    same_country = buyer_country == seller_country

    # Extract net from the VAT-inclusive price
    breakdown = calculate_vat_inclusive(gross_price, seller_vat_rate)

    # Domestic (same country): always charge VAT
    if same_country:
        return AttendeeVATResult(
            effective_price=gross_price,
            net_amount=breakdown.net_amount,
            vat_amount=breakdown.vat_amount,
            vat_rate=breakdown.vat_rate,
            reverse_charge=False,
        )

    # EU cross-border B2B with valid VAT ID: reverse charge
    if buyer_in_eu and buyer_vat_id_valid:
        return AttendeeVATResult(
            effective_price=breakdown.net_amount,
            net_amount=breakdown.net_amount,
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            reverse_charge=True,
        )

    # EU cross-border B2C (no valid VAT ID): seller's VAT rate
    if buyer_in_eu:
        return AttendeeVATResult(
            effective_price=gross_price,
            net_amount=breakdown.net_amount,
            vat_amount=breakdown.vat_amount,
            vat_rate=breakdown.vat_rate,
            reverse_charge=False,
        )

    # Non-EU buyer: no VAT (export)
    return AttendeeVATResult(
        effective_price=breakdown.net_amount,
        net_amount=breakdown.net_amount,
        vat_amount=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
        reverse_charge=False,
    )


def get_effective_vat_rate(tier: "TicketTier", org: "Organization") -> Decimal:
    """Get the applicable VAT rate for a tier, falling back to org default."""
    if tier.vat_rate is not None:
        return tier.vat_rate
    return org.vat_rate


# ---------------------------------------------------------------------------
# VAT preview (used by the vat-preview endpoint)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VATPreviewLineItem:
    """Single line item in a VAT preview result.

    One line per **distinct unit price**, not per requested tier — see
    :func:`calculate_vat_preview`.
    """

    tier_name: str
    ticket_count: int
    unit_price_gross: Decimal
    unit_price_net: Decimal
    unit_vat: Decimal
    vat_rate: Decimal
    line_net: Decimal
    line_vat: Decimal
    line_gross: Decimal
    price_category_name: str | None = None


@dataclass(frozen=True)
class VATPreviewResult:
    """Result of a VAT preview calculation."""

    vat_id_valid: bool | None
    vat_id_validation_error: str | None
    reverse_charge: bool
    line_items: list[VATPreviewLineItem]
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    currency: str


def validate_and_resolve_buyer_country(
    vat_id: str | None,
    vat_country_code: str | None,
) -> tuple[bool | None, str | None, str | None]:
    """Validate a buyer's VAT ID via VIES and resolve their country.

    Core logic shared between VAT preview and checkout flows.
    Derives buyer country from VAT ID prefix only when VIES validates the ID,
    preventing invalid IDs (e.g. "US123") from triggering non-EU zero-rating.

    Args:
        vat_id: The buyer's VAT ID (if provided).
        vat_country_code: Explicit buyer country code (if provided).

    Returns:
        Tuple of (vat_id_valid, vat_id_validation_error, buyer_country).
        buyer_country may be None if neither explicit nor derivable.
    """
    vat_id_valid: bool | None = None
    vat_id_validation_error: str | None = None

    if vat_id:
        from common.service.vies_service import VIESUnavailableError, validate_vat_id_cached

        try:
            result = validate_vat_id_cached(vat_id)
            vat_id_valid = result.valid
        except VIESUnavailableError:
            vat_id_valid = None
            vat_id_validation_error = "VIES validation service temporarily unavailable"
        except ValueError:
            vat_id_valid = False
            vat_id_validation_error = "Invalid VAT ID format"

    # Derive country from VAT ID prefix only if VIES validated it
    buyer_country = vat_country_code
    if not buyer_country and vat_id and len(vat_id) >= 2 and vat_id_valid:
        buyer_country = vat_id[:2].upper()

    return vat_id_valid, vat_id_validation_error, buyer_country


def _validate_buyer_vat(
    billing_info: "BuyerBillingInfoSchema",
    org_country: str,
) -> tuple[bool | None, str | None, str]:
    """Validate buyer VAT ID and resolve buyer country for VAT preview.

    Wraps validate_and_resolve_buyer_country with org_country fallback,
    ensuring the preview always has a country (safe default = full VAT).

    Returns:
        Tuple of (vat_id_valid, vat_id_validation_error, buyer_country).
    """
    vat_id_valid, vat_id_validation_error, buyer_country = validate_and_resolve_buyer_country(
        vat_id=billing_info.vat_id,
        vat_country_code=billing_info.vat_country_code,
    )
    if not buyer_country:
        # Fallback to org country — safe default (same-country = full VAT)
        buyer_country = org_country

    return vat_id_valid, vat_id_validation_error, buyer_country


def _resolve_preview_seats(tier: "TicketTier", item: "VATPreviewItemSchema") -> "list[VenueSeat | None]":
    """Resolve what each ticket of a preview line is priced from, in cart order.

    Two shapes, one per seat-assignment mode, both collapsing onto the same
    ``VenueSeat | None`` vector so everything downstream — pricing, grouping, VAT — is
    mode-blind and the response the frontend renders is identical either way:

    - **user_choice**: the buyer's ``seat_ids``, resolved and scoped exactly like
      checkout's ``_resolve_seats_user_choice`` (the tier's sector, active seats only) so
      a seat the preview prices is a seat checkout would accept. Availability is
      deliberately *not* checked: a preview is not a reservation, and a seat someone else
      is holding is still worth quoting a price for.
    - **best_available**: no seats exist yet, so the line is represented by ``count``
      copies of one *unsaved* seat carrying the requested zone. A best-available request
      is uniformly priced by construction (one zone per request), which is exactly why it
      is representable without seats. The instance is never saved and never read for
      anything but its price category. Any ``seat_ids`` on such a line are ignored, for
      the same reason checkout's ``resolve_seats`` ignores them: the picker assigns the
      seats, not the buyer.

    Args:
        tier: The tier this line buys into.
        item: The requested line — count, and either ``seat_ids`` or ``price_category_id``.

    Returns:
        ``item.count`` entries; ``None`` for general admission / unzoned lines.

    Raises:
        HttpError: 400, if both selectors are supplied, or if any seat id is unknown or
            outside the tier's sector.
        InvalidZoneSelectionError: 400 (rendered by ``events/exception_handlers.py``), if
            the requested zone is unusable on this tier — see
            :func:`~events.service.seating.pick.resolve_requested_zone`, the single
            authority the preview and checkout share so a quote can never be validated
            differently from the charge it predicts.
    """
    from ninja.errors import HttpError

    from events.exceptions import InvalidZoneSelectionError
    from events.models import PriceCategory, TicketTier, VenueSeat
    from events.service.seating.pick import resolve_requested_zone

    if item.seat_ids and item.price_category_id is not None:
        raise HttpError(400, str(_("Provide either seat_ids or price_category_id, not both.")))

    # Asked for EVERY mode, exactly as checkout's `resolve_seats` does. Asking it only when
    # `seat_ids` was empty let a best-available line smuggle itself past the zone rule by
    # sending seats: the preview priced those seats and quoted a total, while the same
    # intent at checkout (which has no seat_ids to send) was refused.
    zone_id = resolve_requested_zone(tier, item.price_category_id)

    if tier.seat_assignment_mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE or not item.seat_ids:
        if zone_id is not None:
            # The preview needs the category itself for the line name; the authority
            # returns only the id, and has already proven it is one of the tier's zones.
            zone = PriceCategory.objects.filter(pk=zone_id).first()
            if zone is None:
                # A zone in the map cannot be deleted (see `delete_price_category`), but that
                # holds at any *instant*, not across two statements: the organizer can unmap
                # the zone and then delete the category between the tier read above and this
                # fetch. Same 400 the buyer gets a moment later, never a 500.
                raise InvalidZoneSelectionError(str(_("That zone is no longer on sale — please choose another.")))
            # One shared read-only stand-in: `resolve_seat_price` reads only the category.
            return [VenueSeat(default_price_category=zone)] * item.count
        # A category-priced user-choice tier has no meaningful flat price, so quoting one would
        # hand the buyer a total checkout will not honour — the exact disagreement this endpoint
        # exists to prevent. Refusing costs no backward compatibility: `category_prices` ships
        # with this feature, so no existing client can be previewing such a tier.
        if tier.category_prices:
            raise HttpError(
                400,
                str(_("This ticket tier prices seats by category — seat_ids are required to preview it.")),
            )
        return [None] * item.count

    seats = VenueSeat.objects.filter(id__in=item.seat_ids, sector_id=tier.sector_id, is_active=True).select_related(
        "default_price_category"
    )
    seat_map = {seat.pk: seat for seat in seats}
    if len(seat_map) != len(set(item.seat_ids)):
        raise HttpError(400, str(_("One or more selected seats are invalid or not in the correct sector.")))
    return [seat_map[seat_id] for seat_id in item.seat_ids]


def _resolve_line_pricing(
    tier: "TicketTier",
    org: "Organization",
    seats: "list[VenueSeat | None]",
    price_per_ticket: Decimal | None,
    discount_code: str | None,
) -> "BatchPricing":
    """Price every ticket in one preview line, per seat (spec §7).

    The priority order is unchanged — PWYC override > discount code > list price — but
    "list price" is now the seat's category price, resolved through the same
    :func:`~events.service.seating.pricing.build_batch_pricing` checkout uses. There is
    deliberately no second resolver: a preview computed by different code than the charge
    is a preview that drifts.

    Two preview-only softenings, both of which only ever move the quote *up* toward the
    real charge:

    - An invalid discount code is ignored (pre-existing behaviour) — the buyer is still
      typing, and a 400 per keystroke is not a preview.
    - A code the cart is too small for is ignored too. Checkout enforces
      ``min_purchase_amount`` against the resolved total (spec §5.6); honouring the code
      here would quote a discount Stripe would never grant.

    A seat painted into a category the tier does not price is **not** softened — see
    :func:`calculate_vat_preview`.

    Args:
        tier: The tier this line buys into.
        org: The selling organization (scopes discount-code lookup).
        seats: Resolved seats in cart order; ``None`` entries are general admission.
        price_per_ticket: The buyer's PWYC amount, if any.
        discount_code: The raw code string the buyer typed, if any.

    Returns:
        The per-ticket price vector for this line.

    Raises:
        HttpError: 400, if the PWYC amount is outside the tier's bounds, or if a seat's
            price category is unpriced.
    """
    from ninja.errors import HttpError

    from events.service import discount_code_service
    from events.service.seating.pricing import build_batch_pricing

    if price_per_ticket is not None:
        if tier.pwyc_min and price_per_ticket < tier.pwyc_min:
            raise HttpError(400, str(_("PWYC amount must be at least {min_amount}")).format(min_amount=tier.pwyc_min))
        if tier.pwyc_max and price_per_ticket > tier.pwyc_max:
            raise HttpError(400, str(_("PWYC amount must be at most {max_amount}")).format(max_amount=tier.pwyc_max))
        return build_batch_pricing(tier, seats, pwyc_amount=price_per_ticket)

    dc = None
    if discount_code:
        try:
            dc = discount_code_service.validate_discount_code_anonymous(discount_code, org, tier)
        except HttpError:
            dc = None  # Invalid discount code in preview — use original price

    pricing = build_batch_pricing(tier, seats, discount_code=dc)
    if dc is not None and pricing.gross_total < dc.min_purchase_amount:
        return build_batch_pricing(tier, seats)
    return pricing


def _group_by_price(
    seats: "list[VenueSeat | None]",
    pricing: "BatchPricing",
) -> dict[tuple["UUID | None", Decimal], tuple[str | None, int]]:
    """Collapse a priced cart line into one entry per (price category, unit price).

    Keyed on the category *id* — two categories may share a name — and on the price, so
    a category that somehow charged two different unit prices would show as two honest
    lines rather than one arbitrary one. Insertion-ordered, so the response mirrors the
    order the buyer picked their seats in.

    Args:
        seats: Resolved seats in cart order; ``None`` for general admission.
        pricing: The per-ticket price vector, positionally aligned with ``seats``.

    Returns:
        ``{(category_id, unit_price): (category_name, ticket_count)}``.
    """
    groups: dict[tuple[UUID | None, Decimal], tuple[str | None, int]] = {}
    for seat, line in zip(seats, pricing.lines, strict=True):
        category = seat.default_price_category if seat is not None else None
        key = (category.pk if category is not None else None, line.unit_price)
        name, count = groups.get(key, (category.name if category is not None else None, 0))
        groups[key] = (name, count + 1)
    return groups


def calculate_vat_preview(
    event: "Event",
    billing_info: "BuyerBillingInfoSchema",
    items: list["VATPreviewItemSchema"],
    discount_code: str | None = None,
    price_per_ticket: Decimal | None = None,
) -> VATPreviewResult:
    """Calculate VAT preview for a set of ticket tiers based on buyer billing info.

    Validates the buyer's VAT ID (via VIES with caching) and computes
    per-line-item and total VAT breakdown. Supports discount codes and
    PWYC price overrides.

    **Seat-aware (spec §7).** An item may carry ``seat_ids`` (user-choice) or a
    ``price_category_id`` zone (best-available, whose seats do not exist until the picker
    runs); each ticket is then priced from that category through the same authority
    checkout uses. Both shapes produce the same response — a zone-priced line is grouped
    and named exactly like a seat-priced one — so the frontend never branches on the
    tier's seating mode to render a quote. Because a category-priced cart has no single
    unit price, the result is grouped into **one line per (price category, unit price)**
    in first-appearance order rather than one line per tier — "2 × Premium @ 80.00,
    1 × Standard @ 30.00" is what an invoice will say, and is what the buyer can check
    against Stripe. Two categories that happen to charge the same price stay two lines;
    merging them would produce an anonymous total the buyer cannot reconcile with the seat
    map. An item with neither selector, on a tier with no category map, yields exactly one
    line at the flat price — byte-identical to the pre-seating behaviour.

    A seat painted into a category the tier does not price raises 400 here, exactly as it
    does at checkout, and deliberately so: this is the one refusal that must **not** be
    softened into a warning. Quoting a total that omits or mis-prices a seat the buyer
    picked is the precise failure this endpoint exists to prevent, and the error names the
    offending category so the buyer can pick another seat or the organizer can fix the map.

    Args:
        event: The event whose tiers are being previewed.
        billing_info: Buyer billing info with optional VAT ID.
        items: List of tier IDs, quantities and optional seat ids / zone id.
        discount_code: Optional discount code to apply.
        price_per_ticket: Optional PWYC price override.

    Returns:
        VATPreviewResult with breakdown.

    Raises:
        HttpError 404: If a tier is not found for the event.
        HttpError 400: On a currency mismatch, an unknown seat, a PWYC amount out of
            bounds, or a seat whose price category the tier does not price.
        InvalidZoneSelectionError: 400, on a missing/unpriced or inapplicable zone —
            raised by the shared authority
            :func:`~events.service.seating.pick.resolve_requested_zone`, so the preview
            refuses a zone in exactly the words checkout would.
    """
    from ninja.errors import HttpError

    from events.models.ticket import TicketTier

    org = event.organization
    vat_id_valid, vat_id_validation_error, buyer_country = _validate_buyer_vat(billing_info, org.vat_country_code)
    buyer_vat_valid = vat_id_valid is True

    line_items: list[VATPreviewLineItem] = []
    total_net = Decimal("0.00")
    total_vat = Decimal("0.00")
    total_gross = Decimal("0.00")
    currency = ""
    reverse_charge = False

    for item in items:
        tier = TicketTier.objects.filter(pk=item.tier_id, event=event).first()
        if not tier:
            raise HttpError(404, "Ticket tier not found.")

        # Validate consistent currency across all tiers
        if not currency:
            currency = tier.currency
        elif tier.currency != currency:
            raise HttpError(400, "All tiers must use the same currency.")

        seats = _resolve_preview_seats(tier, item)
        pricing = _resolve_line_pricing(tier, org, seats, price_per_ticket, discount_code)
        vat_rate = get_effective_vat_rate(tier, org)

        for (_category_id, effective_price), (category_name, count) in _group_by_price(seats, pricing).items():
            vat_result = determine_attendee_vat(
                gross_price=effective_price,
                seller_vat_rate=vat_rate,
                seller_country=org.vat_country_code,
                buyer_country=buyer_country,
                buyer_vat_id_valid=buyer_vat_valid,
            )

            line_net = (vat_result.net_amount * count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            line_vat = (vat_result.vat_amount * count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            line_gross = (vat_result.effective_price * count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

            line_items.append(
                VATPreviewLineItem(
                    tier_name=tier.name,
                    price_category_name=category_name,
                    ticket_count=count,
                    unit_price_gross=effective_price,
                    unit_price_net=vat_result.net_amount,
                    unit_vat=vat_result.vat_amount,
                    vat_rate=vat_result.vat_rate,
                    line_net=line_net,
                    line_vat=line_vat,
                    line_gross=line_gross,
                )
            )

            total_net += line_net
            total_vat += line_vat
            total_gross += line_gross
            if vat_result.reverse_charge:
                reverse_charge = True

    return VATPreviewResult(
        vat_id_valid=vat_id_valid,
        vat_id_validation_error=vat_id_validation_error,
        reverse_charge=reverse_charge,
        line_items=line_items,
        total_net=total_net.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        total_vat=total_vat.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        total_gross=total_gross.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        currency=currency,
    )
