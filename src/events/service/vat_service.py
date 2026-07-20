"""VAT calculation service for ticket sales and platform fees.

Handles in-house VAT calculations for:
- Ticket sales: Organizations charge VAT to buyers (VAT-inclusive pricing)
- Platform fees: B2B fees with EU reverse charge rules

The core B2B VAT determination logic lives in ``common.service.vat_utils``
(shared with the referral payout system).  This module provides the
events-specific wrappers and re-exports.
"""

import typing as t
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

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
    net_platform_fee: Decimal,
    org: "Organization",
    platform_vat_country: str,
    platform_vat_rate: Decimal,
) -> PlatformFeeVATBreakdown:
    """Calculate VAT breakdown for a VAT-exclusive platform fee.

    Thin wrapper around :func:`common.service.vat_utils.calculate_b2b_fee_vat`.

    Args:
        net_platform_fee: Platform fee before VAT.
        org: Organization being billed.
        platform_vat_country: Platform's VAT country code.
        platform_vat_rate: Platform's domestic VAT rate.

    Returns:
        VAT breakdown where ``fee_net`` is the input amount and ``fee_gross``
        includes VAT when applicable.
    """
    return calculate_b2b_fee_vat(net_platform_fee, org, platform_vat_country, platform_vat_rate)


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


def distribute_amount_pro_rata(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split a total in proportion to ``weights``, summing exactly to ``total``.

    Largest-remainder apportionment: every item floors to a whole penny, then the
    leftover pennies go to the items with the biggest fractional parts (ties broken
    by index). Because the fractional parts sum to exactly the number of leftover
    pennies, an item with a zero fractional part — in particular a zero weight —
    never receives one, so a 0.00-priced ticket always gets 0.00.

    Degenerate weights fall back to :func:`distribute_amount_across_items`:

    - **All weights equal** (the uniform cart every batch had before #739): the even
      split *is* the pro-rata split, and delegating keeps the output byte-identical
      to what that helper produced before this function existed.
    - **All weights zero** (a discount drove the whole cart to 0.00): there is no
      proportion to honour, and a fixed platform fee still has to land somewhere.

    Args:
        total: The total amount to distribute.
        weights: One non-negative weight per item (e.g. each ticket's price).

    Returns:
        List of Decimal amounts, one per weight, summing exactly to ``total``.

    Example:
        >>> distribute_amount_pro_rata(Decimal("5.00"), [Decimal("80"), Decimal("20")])
        [Decimal('4.00'), Decimal('1.00')]
    """
    count = len(weights)
    if count == 0:
        return []
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0 or len(set(weights)) == 1:
        return distribute_amount_across_items(total, count)

    # ROUND_FLOOR (not ROUND_DOWN) keeps every fractional part in [0, 1), so the
    # leftover penny count is non-negative even for a negative total.
    exact = [total * weight / total_weight for weight in weights]
    result = [amount.quantize(TWO_PLACES, rounding=ROUND_FLOOR) for amount in exact]
    leftover = int((total - sum(result, Decimal("0"))) / Decimal("0.01"))

    order = sorted(range(count), key=lambda i: (result[i] - exact[i], i))
    for i in order[:leftover]:
        result[i] += Decimal("0.01")

    return result
