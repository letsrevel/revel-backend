"""Shared B2B VAT determination logic.

Provides the core EU reverse-charge / domestic-VAT decision that applies to
any B2B fee or payout.  Domain-specific wrappers live in each app's service
module (e.g. ``events.service.vat_service``, ``accounts.service``).
"""

import typing as t
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from common.constants import EU_MEMBER_STATES

TWO_PLACES = Decimal("0.01")


class VATEntity(t.Protocol):
    """Minimal interface for an entity with VAT information.

    Both ``Organization`` and ``UserBillingProfile`` satisfy this protocol.
    """

    vat_country_code: str
    vat_id: str
    vat_id_validated: bool


@dataclass(frozen=True)
class VATBreakdown:
    """VAT breakdown for an amount with VAT."""

    gross_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    vat_rate: Decimal


@dataclass(frozen=True)
class B2BFeeVATBreakdown:
    """VAT breakdown for a B2B fee or payout (VAT-exclusive semantics)."""

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


def calculate_vat_exclusive(net_amount: Decimal, vat_rate: Decimal) -> VATBreakdown:
    """Calculate VAT breakdown from a VAT-exclusive (net) price.

    Given a net amount and VAT rate, computes the gross amount by adding VAT
    on top.

    Args:
        net_amount: The price excluding VAT.
        vat_rate: The VAT rate as a percentage (e.g., 22.00 for 22%).

    Returns:
        VATBreakdown with net, vat, and gross amounts.
    """
    if vat_rate <= 0:
        return VATBreakdown(
            gross_amount=net_amount,
            net_amount=net_amount,
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )

    vat = (net_amount * vat_rate / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    gross = net_amount + vat

    return VATBreakdown(
        gross_amount=gross,
        net_amount=net_amount,
        vat_amount=vat,
        vat_rate=vat_rate,
    )


def _normalize_vat_country(code: str) -> str:
    """Uppercase a VAT country code and canonicalize Greece (EL → GR).

    VIES syncs Greek entities to the ``EL`` VAT prefix while ISO data uses
    ``GR``; both spellings must compare as the same country.
    """
    normalized = code.upper() if code else ""
    return "GR" if normalized == "EL" else normalized


def b2b_vat_context(
    entity: VATEntity,
    platform_vat_country: str,
    platform_vat_rate: Decimal,
    *,
    unknown_country_domestic: bool = False,
) -> tuple[bool, Decimal]:
    """Return ``(reverse_charge, applicable_vat_rate)`` for a B2B fee.

    ``applicable_vat_rate`` is ``0`` for reverse charge and non-EU entities.

    A blank ``platform_vat_country`` (half-configured SiteSettings) charges no
    VAT and never labels anyone reverse charge — a misconfiguration must not
    produce legally mislabeled invoices.

    Args:
        entity: Any object satisfying :class:`VATEntity`.
        platform_vat_country: The platform's VAT country code (e.g. ``"IT"``).
        platform_vat_rate: The platform's domestic VAT rate (e.g. ``22.00``).
        unknown_country_domestic: When True, an entity with a blank country
            code is charged the platform's domestic VAT instead of falling
            into the non-EU (0%) bucket. Fee-collection paths pass True so an
            org that never filled its billing country is never silently
            zero-rated (the platform would owe that VAT out of its margin);
            payout paths keep the default — when the platform is the
            *customer*, failing safe means NOT crediting VAT to a payee whose
            country is unknown.

    Returns:
        A ``(reverse_charge, applicable_vat_rate)`` pair.
    """
    platform_country = _normalize_vat_country(platform_vat_country)
    if not platform_country:
        return False, Decimal("0.00")

    entity_country = _normalize_vat_country(entity.vat_country_code)
    if not entity_country and unknown_country_domestic:
        return False, platform_vat_rate

    entity_has_valid_vat = bool(entity.vat_id and entity.vat_id_validated)
    entity_in_eu = entity_country in EU_MEMBER_STATES
    same_country = entity_country == platform_country

    if not entity_in_eu:
        # Outside EU: export of services, no VAT
        return False, Decimal("0.00")
    if not same_country and entity_has_valid_vat:
        # EU cross-border B2B with valid VAT ID: reverse charge
        return True, Decimal("0.00")
    # Same country OR EU without valid VAT ID: platform's domestic VAT applies
    return False, platform_vat_rate


def calculate_b2b_fee_vat(
    net_fee: Decimal,
    entity: VATEntity,
    platform_vat_country: str,
    platform_vat_rate: Decimal,
    *,
    unknown_country_domestic: bool = False,
) -> B2BFeeVATBreakdown:
    """Determine VAT treatment for a B2B fee (VAT-exclusive / net amount).

    VAT is added **on top** of the net fee when applicable.  The returned
    ``fee_gross`` includes VAT; ``fee_net`` is always the original ``net_fee``.

    Rules (EU cross-border services, Art. 196 VAT Directive):
        - Entity in **same country** as platform → add domestic VAT on top.
        - Entity in **different EU country** with validated VAT ID → reverse charge.
        - Entity in **EU without** valid VAT ID → add platform domestic VAT on top.
        - Entity **outside EU** → no VAT (export of services).

    Works for both platform-fee invoices (Organization) and referral payout
    statements (UserBillingProfile).

    Args:
        net_fee: The net fee amount (VAT-exclusive).
        entity: Any object satisfying :class:`VATEntity`.
        platform_vat_country: The platform's VAT country code (e.g. ``"IT"``).
        platform_vat_rate: The platform's domestic VAT rate (e.g. ``22.00``).
        unknown_country_domestic: See :func:`b2b_vat_context` — fee-collection
            paths pass True so a blank entity country is charged domestic VAT.

    Returns:
        :class:`B2BFeeVATBreakdown` with fee breakdown and reverse charge flag.
    """
    reverse_charge, rate = b2b_vat_context(
        entity, platform_vat_country, platform_vat_rate, unknown_country_domestic=unknown_country_domestic
    )
    if reverse_charge or rate <= 0:
        return B2BFeeVATBreakdown(
            fee_gross=net_fee,
            fee_net=net_fee,
            fee_vat=Decimal("0.00"),
            fee_vat_rate=Decimal("0.00"),
            reverse_charge=reverse_charge,
        )

    # Add domestic VAT on top
    breakdown = calculate_vat_exclusive(net_fee, rate)
    return B2BFeeVATBreakdown(
        fee_gross=breakdown.gross_amount,
        fee_net=net_fee,
        fee_vat=breakdown.vat_amount,
        fee_vat_rate=rate,
        reverse_charge=False,
    )


def b2b_fee_vat_from_gross(
    gross_fee: Decimal,
    entity: VATEntity,
    platform_vat_country: str,
    platform_vat_rate: Decimal,
    *,
    unknown_country_domestic: bool = False,
) -> B2BFeeVATBreakdown:
    """Decompose a VAT-inclusive (gross) B2B fee into net + VAT.

    Counterpart of :func:`calculate_b2b_fee_vat` for fees where the collected
    amount already includes VAT when applicable (e.g. a Stripe
    ``application_fee_amount`` charged at a VAT-grossed-up percent).

    Args:
        gross_fee: The collected fee amount (VAT-inclusive when VAT applies).
        entity: Any object satisfying :class:`VATEntity`.
        platform_vat_country: The platform's VAT country code (e.g. ``"IT"``).
        platform_vat_rate: The platform's domestic VAT rate (e.g. ``22.00``).
        unknown_country_domestic: See :func:`b2b_vat_context` — fee-collection
            paths pass True so a blank entity country is charged domestic VAT.

    Returns:
        :class:`B2BFeeVATBreakdown` with fee breakdown and reverse charge flag.
    """
    reverse_charge, rate = b2b_vat_context(
        entity, platform_vat_country, platform_vat_rate, unknown_country_domestic=unknown_country_domestic
    )
    if reverse_charge or rate <= 0:
        return B2BFeeVATBreakdown(
            fee_gross=gross_fee,
            fee_net=gross_fee,
            fee_vat=Decimal("0.00"),
            fee_vat_rate=Decimal("0.00"),
            reverse_charge=reverse_charge,
        )

    breakdown = calculate_vat_inclusive(gross_fee, rate)
    return B2BFeeVATBreakdown(
        fee_gross=gross_fee,
        fee_net=breakdown.net_amount,
        fee_vat=breakdown.vat_amount,
        fee_vat_rate=rate,
        reverse_charge=False,
    )
