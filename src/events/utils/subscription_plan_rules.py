"""Shape rules binding a plan's payment method to its price and billing cadence.

Shared by :class:`events.schema.subscription.PlanCreateSchema` (pydantic, create
time) and :func:`events.service.subscription_service.update_plan` (patch time)
so the two enforcement points can never drift. Returns the message rather than
raising so each caller can render it in its own idiom (``ValueError`` → 422 for
pydantic, ``HttpError`` → 400 for the service).
"""

from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from events.models import MembershipSubscriptionPlan, SubscriptionPaymentMethod


def validate_plan_shape(
    *,
    payment_method: str,
    price: Decimal | None,
    period_unit: str,
) -> str | None:
    """Return a validation message when the (method, price, cadence) triple is incoherent.

    Args:
        payment_method: A :class:`SubscriptionPaymentMethod` value.
        price: The plan's price. ``None`` is a client sending an explicit JSON
            ``null`` on PATCH — see the null guard below.
        period_unit: A :class:`MembershipSubscriptionPlan.PeriodUnit` value.

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
        if price <= Decimal("0"):
            return str(_("Online plans must have a price greater than 0."))
        if period_unit == lifetime:
            return str(
                _(
                    "Online plans cannot use the lifetime billing period: Stripe bills monthly or "
                    "yearly. Use an offline plan for a one-off paid membership."
                )
            )
        return None

    # OFFLINE: staff settle the money off-book, so any price >= 0 (enforced by
    # the field validator) and any cadence — including LIFETIME — is coherent.
    return None
