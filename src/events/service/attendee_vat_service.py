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

from common.constants import EU_MEMBER_STATES
from common.service.vat_utils import TWO_PLACES, calculate_vat_inclusive

if t.TYPE_CHECKING:
    from events.models.event import Event
    from events.models.organization import Organization
    from events.models.ticket import TicketTier
    from events.schema.ticket import BuyerBillingInfoSchema, VATPreviewItemSchema


@dataclass(frozen=True)
class AttendeeVATResult:
    """Per-ticket VAT breakdown after applying buyer-specific rules."""

    effective_price: Decimal  # What the buyer actually pays (may differ from tier gross)
    net_amount: Decimal
    vat_amount: Decimal
    vat_rate: Decimal
    reverse_charge: bool


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
    """Single line item in a VAT preview result."""

    tier_name: str
    ticket_count: int
    unit_price_gross: Decimal
    unit_price_net: Decimal
    unit_vat: Decimal
    vat_rate: Decimal
    line_net: Decimal
    line_vat: Decimal
    line_gross: Decimal


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


def _validate_buyer_vat(
    billing_info: "BuyerBillingInfoSchema",
    org_country: str,
) -> tuple[bool | None, str | None, str]:
    """Validate buyer VAT ID and resolve buyer country.

    Returns:
        Tuple of (vat_id_valid, vat_id_validation_error, buyer_country).
    """
    vat_id_valid: bool | None = None
    vat_id_validation_error: str | None = None

    if billing_info.vat_id:
        from common.service.vies_service import VIESUnavailableError, validate_vat_id_cached

        try:
            result = validate_vat_id_cached(billing_info.vat_id)
            vat_id_valid = result.valid
        except VIESUnavailableError:
            vat_id_valid = None
            vat_id_validation_error = "VIES validation service temporarily unavailable"
        except ValueError:
            vat_id_valid = False
            vat_id_validation_error = "Invalid VAT ID format"

    # Derive country from VAT ID prefix only if VIES validated it
    buyer_country = billing_info.vat_country_code
    if not buyer_country and billing_info.vat_id and len(billing_info.vat_id) >= 2 and vat_id_valid:
        buyer_country = billing_info.vat_id[:2].upper()
    if not buyer_country:
        # Fallback to org country — safe default (same-country = full VAT)
        buyer_country = org_country

    return vat_id_valid, vat_id_validation_error, buyer_country


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

    Args:
        event: The event whose tiers are being previewed.
        billing_info: Buyer billing info with optional VAT ID.
        items: List of tier IDs and quantities.
        discount_code: Optional discount code to apply.
        price_per_ticket: Optional PWYC price override.

    Returns:
        VATPreviewResult with breakdown.

    Raises:
        HttpError 404: If a tier is not found for the event.
    """
    from ninja.errors import HttpError

    from events.models.ticket import TicketTier
    from events.service import discount_code_service

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

        # Determine effective price: PWYC override > discount > tier price
        effective_price = tier.price
        if price_per_ticket is not None:
            effective_price = price_per_ticket
        elif discount_code:
            try:
                dc = discount_code_service.validate_discount_code_anonymous(discount_code, org, tier)
                effective_price = discount_code_service.calculate_discounted_price(tier, dc)
            except HttpError:
                pass  # Invalid discount code in preview — use original price

        vat_rate = get_effective_vat_rate(tier, org)
        vat_result = determine_attendee_vat(
            gross_price=effective_price,
            seller_vat_rate=vat_rate,
            seller_country=org.vat_country_code,
            buyer_country=buyer_country,
            buyer_vat_id_valid=buyer_vat_valid,
        )

        line_net = (vat_result.net_amount * item.count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        line_vat = (vat_result.vat_amount * item.count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        line_gross = (vat_result.effective_price * item.count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        line_items.append(
            VATPreviewLineItem(
                tier_name=tier.name,
                ticket_count=item.count,
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
        currency = tier.currency
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
