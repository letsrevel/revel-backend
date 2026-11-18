"""Shape rules binding a plan's payment method to its price and billing cadence.

Shared by :class:`events.schema.subscription_plan.PlanCreateSchema` (pydantic, create
time) and :func:`events.service.subscription.plans.update_plan` (patch time)
so the two enforcement points can never drift. Returns the message rather than
raising so each caller can render it in its own idiom (``ValueError`` → 422 for
pydantic, ``HttpError`` → 400 for the service).
"""

from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from events.models import MembershipSubscriptionPlan, SubscriptionPaymentMethod


def _calendar_shape_error(
    period_alignment: str, period_unit: str, period_count: int, calendar_start_month: int
) -> str | None:
    """Return a message when the calendar-alignment settings are incoherent with the cadence."""
    calendar = period_alignment == MembershipSubscriptionPlan.PeriodAlignment.CALENDAR
    if calendar:
        if period_unit not in (
            MembershipSubscriptionPlan.PeriodUnit.MONTH,
            MembershipSubscriptionPlan.PeriodUnit.YEAR,
        ):
            return str(_("Calendar plans must use a monthly or yearly billing period."))
        if period_count != 1:
            return str(_("Calendar plans must have a period count of 1."))
    if calendar_start_month != 1 and not (calendar and period_unit == MembershipSubscriptionPlan.PeriodUnit.YEAR):
        return str(_("A calendar start month other than January is only allowed on calendar yearly plans."))
    return None


def _online_shape_error(
    price: Decimal, period_unit: str, period_alignment: str, first_period_pricing: str
) -> str | None:
    """Return a message when an ONLINE plan's shape is not Stripe-billable."""
    # ponytail: lift this one rule when Stripe billing-cycle-anchor support lands (#1426).
    if period_alignment == MembershipSubscriptionPlan.PeriodAlignment.CALENDAR:
        return str(_("Calendar-aligned plans are not available for online payments yet."))
    if first_period_pricing == MembershipSubscriptionPlan.FirstPeriodPricing.FULL:
        return str(_("Online plans can only prorate the first period."))
    if price <= Decimal("0"):
        return str(_("Online plans must have a price greater than 0."))
    if period_unit == MembershipSubscriptionPlan.PeriodUnit.LIFETIME:
        return str(
            _(
                "Online plans cannot use the lifetime billing period: Stripe bills monthly or "
                "yearly. Use an offline plan for a one-off paid membership."
            )
        )
    return None


def validate_plan_shape(
    *,
    payment_method: str,
    price: Decimal | None,
    period_unit: str,
    period_alignment: str,
    period_count: int,
    calendar_start_month: int,
    first_period_pricing: str | None,
) -> str | None:
    """Return a validation message when the plan's shape is incoherent.

    Checks the payment method against the price and cadence, the calendar-alignment
    settings against the cadence, and the first-period pricing against the method.

    Args:
        payment_method: A :class:`SubscriptionPaymentMethod` value.
        price: The plan's price. ``None`` is a client sending an explicit JSON
            ``null`` on PATCH — see the null guard below.
        period_unit: A :class:`MembershipSubscriptionPlan.PeriodUnit` value.
        period_alignment: A :class:`MembershipSubscriptionPlan.PeriodAlignment` value.
        period_count: Billing units per period.
        calendar_start_month: Month (1-12) a calendar YEAR period starts on.
        first_period_pricing: A :class:`MembershipSubscriptionPlan.FirstPeriodPricing` value.
            ``None`` is an explicit JSON ``null`` on PATCH (same guard as ``price``).

    Returns:
        The error message, or ``None`` when the combination is valid.
    """
    lifetime = MembershipSubscriptionPlan.PeriodUnit.LIFETIME

    # ``PlanUpdateSchema.price`` is ``Decimal | None``, and pydantic skips the
    # ``ge`` constraint for None — so an explicit ``{"price": null}`` reaches
    # ``update_plan`` as a *present* key holding None, slips past
    # ``fields.get("price", plan.price)``, and used to hit ``None <= Decimal``
    # here (TypeError → 500). The column is NOT NULL, so this is simply an
    # invalid patch: refuse it as a 400 like any other bad shape.
    if price is None:
        return str(_("Price cannot be null."))
    if first_period_pricing is None:
        return str(_("First period pricing cannot be null."))

    message = _calendar_shape_error(period_alignment, period_unit, period_count, calendar_start_month)
    if message:
        return message

    if payment_method == SubscriptionPaymentMethod.FREE:
        if price != Decimal("0"):
            return str(_("Free plans must have a price of 0."))
        # ponytail: FREE is deliberately capped at LIFETIME. A finite free term
        # (e.g. a free trial year) would be selected by the lapse beat and
        # expire with no way for the member to renew — there is no payment to
        # record. Supporting one means teaching the beat to auto-renew
        # zero-price periods instead of dunning them.
        if period_unit != lifetime:
            return str(_("Free plans must use the lifetime billing period."))
        return None

    if payment_method == SubscriptionPaymentMethod.ONLINE:
        return _online_shape_error(price, period_unit, period_alignment, first_period_pricing)

    # OFFLINE: staff settle the money off-book, so any price >= 0 (enforced by
    # the field validator) and any cadence — including LIFETIME — is coherent.
    return None
