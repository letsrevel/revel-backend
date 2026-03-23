"""VAT calculation service for ticket sales and platform fees.

Handles in-house VAT calculations for:
- Ticket sales: Organizations charge VAT to buyers (VAT-inclusive pricing)
- Platform fees: B2B fees with EU reverse charge rules

The core B2B VAT determination logic lives in ``common.service.vat_utils``
(shared with the referral payout system).  This module provides the
events-specific wrappers and re-exports.
"""

import typing as t
from decimal import ROUND_HALF_UP, Decimal

from common.service.vat_utils import (
    TWO_PLACES,
    B2BFeeVATBreakdown,
    calculate_b2b_fee_vat,
)
from common.service.vat_utils import (
    VATBreakdown as VATBreakdown,
)
from common.service.vat_utils import (
    calculate_vat_inclusive as calculate_vat_inclusive,
)

if t.TYPE_CHECKING:
    from events.models.organization import Organization

# Re-export the old name for backwards compatibility.
PlatformFeeVATBreakdown = B2BFeeVATBreakdown


def calculate_platform_fee_vat(
    platform_fee: Decimal,
    org: "Organization",
    platform_vat_country: str,
    platform_vat_rate: Decimal,
) -> PlatformFeeVATBreakdown:
    """Calculate VAT breakdown for a platform fee (already VAT-inclusive).

    Thin wrapper around :func:`common.service.vat_utils.calculate_b2b_fee_vat`.
    """
    return calculate_b2b_fee_vat(platform_fee, org, platform_vat_country, platform_vat_rate)


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
