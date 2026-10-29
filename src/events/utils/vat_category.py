"""EN 16931 VAT category derivation for attendee invoice lines.

Pure utility: no DB access, no imports from services or models.
"""

import enum
import typing as t
from decimal import Decimal

from events.utils.tier_pricing import parse_price_map

if t.TYPE_CHECKING:
    from events.models import Organization, TicketTier


EXEMPT_FALLBACK_TEXT = "Exempt from VAT"


class VatCategory(enum.StrEnum):
    """EN 16931 VAT category codes (UNCL5305) used on invoice lines."""

    STANDARD = "S"
    REVERSE_CHARGE = "AE"
    EXEMPT = "E"
    EXPORT = "G"


class VatExemptionDict(t.TypedDict):
    """Exemption text and code printed for a VAT category on an invoice."""

    text: str
    code: str


def derive_vat_category(rate: Decimal, reverse_charge: bool) -> VatCategory:
    """Derive the EN 16931 VAT category of an invoice line.

    Args:
        rate: The VAT rate in percent (0 means no VAT is charged).
        reverse_charge: Whether the supply is subject to reverse charge.

    Returns:
        ``REVERSE_CHARGE`` when ``reverse_charge`` is set, ``STANDARD`` for a positive
        rate, otherwise ``EXEMPT``.
    """
    if reverse_charge:
        return VatCategory.REVERSE_CHARGE
    if rate > 0:
        return VatCategory.STANDARD
    return VatCategory.EXEMPT


def build_vat_exemptions(
    categories: t.Iterable[VatCategory], exempt_reasons: t.Iterable[str]
) -> dict[str, VatExemptionDict]:
    """Build the invoice's VAT exemption map keyed by category code.

    Standard-rated lines need no exemption text, so ``S`` never gets an entry.

    Args:
        categories: The VAT categories of the invoice's lines.
        exempt_reasons: Candidate exemption reasons for exempt lines, in priority order.
            Blank entries are ignored and duplicates are dropped (first occurrence wins).

    Returns:
        A dict with an ``AE`` entry when any line is reverse-charged and an ``E`` entry
        when any line is exempt. The ``E`` text joins the distinct reasons with ``"; "``,
        or falls back to ``"Exempt from VAT"`` when none are given.
    """
    category_set = set(categories)
    exemptions: dict[str, VatExemptionDict] = {}
    if VatCategory.REVERSE_CHARGE in category_set:
        exemptions[VatCategory.REVERSE_CHARGE] = {"text": "Reverse charge", "code": "VATEX-EU-AE"}
    if VatCategory.EXEMPT in category_set:
        reasons = list(dict.fromkeys(r.strip() for r in exempt_reasons if r.strip()))
        exemptions[VatCategory.EXEMPT] = {"text": "; ".join(reasons) or EXEMPT_FALLBACK_TEXT, "code": ""}
    return exemptions


def effective_vat_exemption_reason(tier: "TicketTier", org: "Organization") -> str:
    """Return the exemption reason that applies to a tier: its own, else the organization's default.

    Returns:
        The stripped reason, or ``""`` when neither is set.
    """
    return tier.vat_exemption_reason.strip() or org.vat_exemption_reason.strip()


# Tier fields whose change can bring a tier under the "0% rate needs an exemption reason" rule.
ZERO_RATE_RULE_FIELDS = frozenset(
    {"vat_rate", "vat_exemption_reason", "payment_method", "price", "price_type", "pwyc_min", "pwyc_max", "category_prices"}
)


def is_paid_online(tier: "TicketTier") -> bool:
    """Whether a tier sells for money online, i.e. whether it can ever produce an attendee invoice.

    Free and offline tiers never reach Stripe, so they never get an invoice. Values may still be
    strings on an unsaved tier built from a JSON payload, hence the ``Decimal(str(...))`` coercion.

    Returns:
        ``True`` for an ONLINE tier with a positive fixed price, or a pay-what-you-can tier with
        a positive minimum or maximum, or a seated tier with any positive category price.
    """
    if tier.payment_method != "online":  # TicketTier.PaymentMethod.ONLINE; models are not imported here
        return False
    if tier.price_type == "pwyc":
        return any(Decimal(str(v)) > 0 for v in (tier.pwyc_min, tier.pwyc_max) if v is not None)
    if Decimal(str(tier.price)) > 0:
        return True
    return any(price > 0 for price in parse_price_map(tier.category_prices).values())
