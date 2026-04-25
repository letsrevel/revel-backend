"""Currency helpers for Stripe amount scaling.

Stripe expects amounts in the smallest currency unit. For 2-decimal currencies
(USD, EUR, ...) that is cents (major × 100). For zero-decimal currencies
(JPY, KRW, VND, ...) that is the integer amount itself (no scaling). This
module centralizes the distinction so callers do not hand-roll ``amount * 100``
with the wrong result for zero-decimal currencies.

Reference: https://docs.stripe.com/currencies#zero-decimal
"""

from decimal import ROUND_HALF_UP, Decimal

# Zero-decimal currencies per Stripe's docs. HUF and TWD are two-decimal for
# refund/charge purposes despite being "no subunit" in practice, so they are
# intentionally NOT in this set.
_ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)


def _is_zero_decimal(currency: str) -> bool:
    return currency.upper() in _ZERO_DECIMAL_CURRENCIES


def to_stripe_amount(amount: Decimal, currency: str) -> int:
    """Convert a major-unit amount to Stripe's smallest-unit integer representation.

    Half-up rounding is applied to fractional minor units (matches the
    ROUND_HALF_UP convention used in stripe_service.py for platform-fee and VAT
    arithmetic — see PR #359). Plain ``int(...)`` truncates toward zero, which
    under-charges or under-refunds by 1 minor unit on amounts ending in ``...X5``.

    Args:
        amount: Amount in major currency units (e.g., ``Decimal("40.00")`` EUR).
        currency: ISO 4217 currency code (case-insensitive).

    Returns:
        Integer amount in the currency's smallest unit.
    """
    scaled = amount if _is_zero_decimal(currency) else amount * Decimal(100)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def from_stripe_amount(amount: int, currency: str) -> Decimal:
    """Convert a Stripe smallest-unit integer to a major-unit Decimal.

    Args:
        amount: Amount in the currency's smallest unit as returned by Stripe.
        currency: ISO 4217 currency code (case-insensitive).

    Returns:
        Decimal amount in major currency units.
    """
    if _is_zero_decimal(currency):
        return Decimal(amount)
    return Decimal(amount) / Decimal(100)
