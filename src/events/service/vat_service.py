"""VAT calculation service for ticket sales and platform fees.

Handles in-house VAT calculations for:
- Ticket sales: Organizations charge VAT to buyers (VAT-inclusive pricing)
- Platform fees: B2B fees with EU reverse charge rules
"""

import typing as t
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

if t.TYPE_CHECKING:
    from events.models.organization import Organization

EU_MEMBER_STATES: frozenset[str] = frozenset(
    {
        "AT",  # Austria
        "BE",  # Belgium
        "BG",  # Bulgaria
        "CY",  # Cyprus
        "CZ",  # Czech Republic
        "DE",  # Germany
        "DK",  # Denmark
        "EE",  # Estonia
        "ES",  # Spain
        "FI",  # Finland
        "FR",  # France
        "GR",  # Greece
        "HR",  # Croatia
        "HU",  # Hungary
        "IE",  # Ireland
        "IT",  # Italy
        "LT",  # Lithuania
        "LU",  # Luxembourg
        "LV",  # Latvia
        "MT",  # Malta
        "NL",  # Netherlands
        "PL",  # Poland
        "PT",  # Portugal
        "RO",  # Romania
        "SE",  # Sweden
        "SI",  # Slovenia
        "SK",  # Slovakia
    }
)

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class VATBreakdown:
    """VAT breakdown for a VAT-inclusive price."""

    gross_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    vat_rate: Decimal


@dataclass(frozen=True)
class PlatformFeeVATBreakdown:
    """VAT breakdown for a platform fee."""

    fee_gross: Decimal
    fee_net: Decimal
    fee_vat: Decimal
    fee_vat_rate: Decimal
    reverse_charge: bool


def calculate_vat_inclusive(gross_amount: Decimal, vat_rate: Decimal) -> VATBreakdown:
    """Calculate VAT breakdown from a VAT-inclusive price.

    Args:
        gross_amount: The total price including VAT.
        vat_rate: The VAT rate as a percentage (e.g., 22.00 for 22%).

    Returns:
        VATBreakdown with net, vat, and gross amounts.
    """
    if vat_rate <= 0:
        return VATBreakdown(
            gross_amount=gross_amount,
            net_amount=gross_amount,
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )

    net = (gross_amount / (1 + vat_rate / 100)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    vat = gross_amount - net

    return VATBreakdown(
        gross_amount=gross_amount,
        net_amount=net,
        vat_amount=vat,
        vat_rate=vat_rate,
    )


def calculate_platform_fee_vat(
    platform_fee: Decimal,
    org: "Organization",
    platform_vat_country: str,
    platform_vat_rate: Decimal,
) -> PlatformFeeVATBreakdown:
    """Calculate VAT breakdown for a platform fee (already VAT-inclusive).

    Rules:
    - Org in same country as platform: Extract domestic VAT from fee.
    - Org in different EU country with valid VAT ID: Reverse charge (fee = net, no VAT).
    - Org in EU without valid VAT ID: Extract platform's domestic VAT rate from fee.
    - Org outside EU: No VAT (fee = net, export of services).

    Args:
        platform_fee: The gross platform fee (VAT-inclusive).
        org: The organization being charged.
        platform_vat_country: The platform's VAT country code (e.g., "IT").
        platform_vat_rate: The platform's domestic VAT rate (e.g., 22.00).

    Returns:
        PlatformFeeVATBreakdown with fee breakdown and reverse charge flag.
    """
    org_country = org.vat_country_code.upper() if org.vat_country_code else ""
    org_has_valid_vat = bool(org.vat_id and org.vat_id_validated)
    org_in_eu = org_country in EU_MEMBER_STATES
    same_country = org_country == platform_vat_country.upper()

    if not org_in_eu:
        # Outside EU: export of services, no VAT
        return PlatformFeeVATBreakdown(
            fee_gross=platform_fee,
            fee_net=platform_fee,
            fee_vat=Decimal("0.00"),
            fee_vat_rate=Decimal("0.00"),
            reverse_charge=False,
        )

    if org_in_eu and not same_country and org_has_valid_vat:
        # EU cross-border B2B with valid VAT ID: reverse charge
        # The fee is treated as net (org accounts for VAT via reverse charge)
        return PlatformFeeVATBreakdown(
            fee_gross=platform_fee,
            fee_net=platform_fee,
            fee_vat=Decimal("0.00"),
            fee_vat_rate=Decimal("0.00"),
            reverse_charge=True,
        )

    # Same country OR EU without valid VAT ID: extract domestic VAT
    breakdown = calculate_vat_inclusive(platform_fee, platform_vat_rate)
    return PlatformFeeVATBreakdown(
        fee_gross=platform_fee,
        fee_net=breakdown.net_amount,
        fee_vat=breakdown.vat_amount,
        fee_vat_rate=platform_vat_rate,
        reverse_charge=False,
    )


def get_effective_vat_rate(tier_vat_rate: Decimal | None, org_vat_rate: Decimal) -> Decimal:
    """Return the effective VAT rate for a ticket tier.

    Uses the tier's override if set, otherwise falls back to the org default.
    """
    if tier_vat_rate is not None:
        return tier_vat_rate
    return org_vat_rate


def distribute_amount_across_items(total: Decimal, count: int) -> list[Decimal]:
    """Split a total amount across N items, distributing remainder pennies.

    Ensures the sum of returned values exactly equals total.
    Extra pennies are added to the first item(s).

    Args:
        total: The total amount to distribute.
        count: Number of items to distribute across.

    Returns:
        List of Decimal amounts that sum exactly to total.

    Example:
        >>> distribute_amount_across_items(Decimal("0.10"), 3)
        [Decimal('0.04'), Decimal('0.03'), Decimal('0.03')]
    """
    if count <= 0:
        return []
    if count == 1:
        return [total]

    base = (total / count).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    remainder = total - (base * count)

    # remainder will be a small amount like 0.01 or -0.01
    # Distribute by adjusting the first item(s)
    result = [base] * count

    # Adjust for rounding: add/subtract pennies from the first items
    penny = Decimal("0.01") if remainder > 0 else Decimal("-0.01")
    adjustments = abs(remainder / Decimal("0.01")).to_integral_value()

    for i in range(int(adjustments)):
        result[i] += penny

    return result
