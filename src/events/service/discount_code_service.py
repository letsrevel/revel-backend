"""Service layer for discount code validation and application."""

import typing as t
from decimal import Decimal
from uuid import UUID

import structlog
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, Ticket, TicketTier
from events.models.discount_code import DiscountCode

if t.TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser

    from events import schema
    from events.schema.discount_code import DiscountCodeCreateSchema, DiscountCodeUpdateSchema

logger = structlog.get_logger(__name__)

# Mapping from schema field name → (model M2M attr name, related model class)
_M2M_FIELDS: dict[str, tuple[str, type[Event] | type[EventSeries] | type[TicketTier]]] = {
    "series_ids": ("series", EventSeries),
    "event_ids": ("events", Event),
    "tier_ids": ("tiers", TicketTier),
}


def _set_m2m_relations(dc: DiscountCode, m2m_data: dict[str, list[UUID]]) -> None:
    """Set M2M relationships on a discount code from a {schema_field: ids} dict."""
    for schema_field, ids in m2m_data.items():
        attr_name, model_cls = _M2M_FIELDS[schema_field]
        getattr(dc, attr_name).set(model_cls.objects.filter(id__in=ids))


def create_discount_code(
    organization: Organization,
    payload: "DiscountCodeCreateSchema",
) -> DiscountCode:
    """Create a discount code with optional M2M scope relations.

    Args:
        organization: The owning organization.
        payload: Validated create schema.

    Returns:
        The created DiscountCode instance.
    """
    data = payload.model_dump()
    data["code"] = data["code"].upper()
    # Always pop M2M keys (they aren't model fields); keep only truthy values for .set()
    m2m_data = {key: val for key in _M2M_FIELDS if (val := data.pop(key, None))}
    dc = DiscountCode.objects.create(organization=organization, **data)
    _set_m2m_relations(dc, m2m_data)
    return dc


def update_discount_code(
    dc: DiscountCode,
    payload: "DiscountCodeUpdateSchema",
) -> DiscountCode:
    """Update a discount code's scalar fields and M2M scope relations.

    Args:
        dc: The existing DiscountCode instance.
        payload: Validated update schema (exclude_unset semantics).

    Returns:
        The refreshed DiscountCode instance with prefetched M2M.
    """
    from events.service import update_db_instance

    data = payload.model_dump(exclude_unset=True)
    m2m_data = {key: data.pop(key) for key in list(_M2M_FIELDS) if key in data}

    # Update scalar fields with race-condition-safe locking
    if data:
        dc = update_db_instance(dc, **data)

    _set_m2m_relations(dc, m2m_data)

    return DiscountCode.objects.prefetch_related("series", "events", "tiers").get(pk=dc.pk)


def _check_scope_applicability(dc: DiscountCode, tier: TicketTier) -> None:
    """Check if the discount code applies to the given tier (union logic).

    Raises:
        HttpError: If the discount code is not applicable to this tier.
    """
    has_scope = dc.tiers.exists() or dc.events.exists() or dc.series.exists()
    if not has_scope:
        return  # No scope narrowing = org-wide

    tier_match = dc.tiers.filter(pk=tier.pk).exists()
    event_match = dc.events.filter(pk=tier.event_id).exists()
    series_match = tier.event.event_series_id is not None and dc.series.filter(pk=tier.event.event_series_id).exists()
    if not (tier_match or event_match or series_match):
        raise HttpError(400, str(_("This discount code is not valid for this ticket tier.")))


def _validate_core(
    code: str,
    organization: Organization,
    tier: TicketTier,
    batch_size: int = 1,
) -> DiscountCode:
    """Core validation shared by authenticated and anonymous flows.

    Checks: lookup, dates, global usage limit (optimistic), tier type,
    scope applicability, currency match, and minimum purchase amount.

    Args:
        code: The discount code string.
        organization: The organization owning the event.
        tier: The ticket tier being purchased.
        batch_size: Number of tickets in the purchase.

    Returns:
        The validated DiscountCode instance.

    Raises:
        HttpError: If the discount code is invalid or not applicable.
    """
    now = timezone.now()

    dc = get_object_or_404(DiscountCode, code=code.upper(), organization=organization, is_active=True)

    # Date validity
    if dc.valid_from and dc.valid_from > now:
        raise HttpError(400, str(_("This discount code is not yet active.")))
    if dc.valid_until and dc.valid_until < now:
        raise HttpError(400, str(_("This discount code has expired.")))

    # Global usage limit (optimistic — definitive check under lock in apply_discount)
    if dc.max_uses is not None and dc.times_used >= dc.max_uses:
        raise HttpError(400, str(_("This discount code has reached its usage limit.")))

    # Tier type checks
    if tier.payment_method == TicketTier.PaymentMethod.FREE:
        raise HttpError(400, str(_("Discount codes cannot be applied to free tickets.")))
    if tier.price_type == TicketTier.PriceType.PWYC:
        raise HttpError(400, str(_("Discount codes cannot be applied to pay-what-you-can tickets.")))

    _check_scope_applicability(dc, tier)

    # Currency match for FIXED_AMOUNT
    if dc.discount_type == DiscountCode.DiscountType.FIXED_AMOUNT and dc.currency != tier.currency:
        raise HttpError(400, str(_("This discount code is not valid for this currency.")))

    # Minimum purchase amount
    total_amount = tier.price * batch_size
    if total_amount < dc.min_purchase_amount:
        raise HttpError(
            400,
            str(_("Minimum purchase amount of {amount} required to use this discount code.")).format(
                amount=dc.min_purchase_amount,
            ),
        )

    return dc


def _check_per_user_usage(dc: DiscountCode, user: RevelUser, batch_size: int) -> None:
    """Optimistic per-user usage limit check.

    Raises:
        HttpError: If per-user usage limit would be exceeded.
    """
    user_usage = Ticket.objects.filter(
        discount_code=dc,
        user=user,
        status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
    ).count()
    if user_usage + batch_size > dc.max_uses_per_user:
        raise HttpError(400, str(_("You have already used this discount code the maximum number of times.")))


def validate_discount_code(
    code: str,
    organization: Organization,
    tier: TicketTier,
    user: RevelUser,
    batch_size: int,
) -> DiscountCode:
    """Validate a discount code for a specific purchase.

    Performs optimistic checks (dates, limits, scope, etc.). The definitive
    usage-limit check happens under lock in apply_discount().

    Args:
        code: The discount code string.
        organization: The organization owning the event.
        tier: The ticket tier being purchased.
        user: The purchasing user.
        batch_size: Number of tickets in the purchase.

    Returns:
        The validated DiscountCode instance.

    Raises:
        HttpError: If the discount code is invalid or not applicable.
    """
    dc = _validate_core(code, organization, tier, batch_size)

    # Per-user usage limit (optimistic — definitive check under lock in apply_discount)
    _check_per_user_usage(dc, user, batch_size)

    logger.info(
        "discount_code_validated",
        code=dc.code,
        user_id=str(user.id),
        tier_id=str(tier.id),
        discount_type=dc.discount_type,
        discount_value=str(dc.discount_value),
    )

    return dc


def validate_discount_code_anonymous(
    code: str,
    organization: Organization,
    tier: TicketTier,
) -> DiscountCode:
    """Validate a discount code for anonymous (guest) users.

    Skips per-user limit checks since there is no authenticated user.

    Args:
        code: The discount code string.
        organization: The organization owning the event.
        tier: The ticket tier being purchased.

    Returns:
        The validated DiscountCode instance.

    Raises:
        HttpError: If the discount code is invalid or not applicable.
    """
    return _validate_core(code, organization, tier, batch_size=1)


def preview_discount_code(
    code: str,
    organization: Organization,
    tier: TicketTier,
    user: "RevelUser | AnonymousUser",
) -> "schema.DiscountCodeValidationResponse":
    """Validate a discount code and return a preview of the discounted price.

    Handles both authenticated and anonymous users. Does not decrement usage.

    Args:
        code: The discount code string.
        organization: The organization owning the event.
        tier: The ticket tier.
        user: The user (may be anonymous).

    Returns:
        DiscountCodeValidationResponse with discount details.

    Raises:
        HttpError: If the discount code is invalid or not applicable.
    """
    from events import schema

    if user.is_anonymous:
        dc = validate_discount_code_anonymous(code, organization, tier)
    else:
        dc = validate_discount_code(code, organization, tier, user, batch_size=1)

    discounted_price = calculate_discounted_price(tier, dc)
    return schema.DiscountCodeValidationResponse(
        valid=True,
        discount_type=DiscountCode.DiscountType(dc.discount_type),
        discount_value=dc.discount_value,
        discounted_price=discounted_price,
    )


def calculate_discounted_price(tier: TicketTier, discount_code: DiscountCode) -> Decimal:
    """Calculate the discounted price for a tier.

    Args:
        tier: The ticket tier.
        discount_code: The validated discount code.

    Returns:
        The discounted unit price.
    """
    if discount_code.discount_type == DiscountCode.DiscountType.PERCENTAGE:
        discounted = (tier.price * (Decimal("100") - discount_code.discount_value) / Decimal("100")).quantize(
            Decimal("0.01")
        )
        return max(discounted, Decimal("0"))

    # FIXED_AMOUNT
    return max(tier.price - discount_code.discount_value, Decimal("0"))


def calculate_discount_amount(tier: TicketTier, discount_code: DiscountCode) -> Decimal:
    """Calculate the discount amount (how much is subtracted from the original price).

    Args:
        tier: The ticket tier.
        discount_code: The validated discount code.

    Returns:
        The discount amount per ticket.
    """
    return tier.price - calculate_discounted_price(tier, discount_code)


def apply_discount(discount_code: DiscountCode, user: RevelUser, batch_size: int) -> None:
    """Atomically verify limits and increment the usage counter.

    Must be called inside @transaction.atomic. Uses select_for_update to
    serialize concurrent access and prevent exceeding usage limits.

    Args:
        discount_code: The discount code to increment.
        user: The purchasing user.
        batch_size: Number of tickets purchased.

    Raises:
        HttpError: If usage limits are exceeded (rolls back the transaction).
    """
    dc = DiscountCode.objects.select_for_update().get(pk=discount_code.pk)

    # Re-check global limit under lock
    if dc.max_uses is not None and dc.times_used + batch_size > dc.max_uses:
        raise HttpError(400, str(_("This discount code has reached its usage limit.")))

    # Re-check per-user limit under lock
    user_usage = Ticket.objects.filter(
        discount_code=dc,
        user=user,
        status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
    ).count()
    if user_usage + batch_size > dc.max_uses_per_user:
        raise HttpError(400, str(_("You have already used this discount code the maximum number of times.")))

    DiscountCode.objects.filter(pk=dc.pk).update(times_used=F("times_used") + batch_size)
    logger.info(
        "discount_code_applied",
        code=discount_code.code,
        batch_size=batch_size,
    )
