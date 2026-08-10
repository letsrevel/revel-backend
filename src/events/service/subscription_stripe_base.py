"""Shared Stripe provisioning primitives for membership subscriptions.

Bottom of the subscription Stripe stack (next to
:mod:`subscription_stripe_payloads`): helpers needed by both
:mod:`subscription_stripe_service` and :mod:`subscription_stripe_plan_change`,
extracted so the two can depend on this module instead of on each other.
"""

import typing as t

import stripe
import structlog
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import MembershipSubscriptionPlan, Organization
from events.service.subscription_stripe_payloads import _stripe_account_kwargs
from events.utils.currency import to_stripe_amount

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service):
# this module makes its own outbound calls and must not rely on another
# module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION
# Same reasoning for the HTTP timeout (see stripe_service): don't rely on
# another module's import to configure stripe.default_http_client.
stripe.default_http_client = stripe.RequestsClient(  # type: ignore[attr-defined]
    timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS
)


def _require_stripe_connected(organization: Organization) -> None:
    """Raise 400 if the organization has not finished Stripe Connect onboarding."""
    if not organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))


def _price_inputs_changed(plan: MembershipSubscriptionPlan, price: stripe.Price) -> bool:
    """True when ``plan``'s pricing inputs no longer match the Stripe Price."""
    if not price.active:
        return True
    if price.unit_amount != to_stripe_amount(plan.price, plan.currency):
        return True
    if (price.currency or "").upper() != plan.currency.upper():
        return True
    recurring = price.recurring or {}
    if recurring.get("interval") != plan.period_unit:
        return True
    if recurring.get("interval_count") != plan.period_count:
        return True
    return False


def ensure_stripe_price(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Create or sync the Stripe Product + Price for an ONLINE plan.

    Stripe Prices are immutable on the dimensions we care about (unit amount,
    currency, recurring interval). When any of those change we archive the
    existing Price and create a fresh one.

    A no-op for OFFLINE plans.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return plan

    org = plan.tier.organization
    _require_stripe_connected(org)
    kwargs = _stripe_account_kwargs(org)
    update_fields: list[str] = []

    try:
        if not plan.stripe_product_id:
            product = stripe.Product.create(
                # Generic label only — tier/plan names and descriptions never reach Stripe (#848).
                name="Membership",
                metadata={"revel_plan_id": str(plan.pk)},
                **kwargs,
            )
            plan.stripe_product_id = t.cast(str, product.id)
            update_fields.append("stripe_product_id")

        needs_new_price = not plan.stripe_price_id
        if not needs_new_price:
            existing_price = stripe.Price.retrieve(plan.stripe_price_id, **kwargs)
            if _price_inputs_changed(plan, existing_price):
                if existing_price.active:
                    stripe.Price.modify(plan.stripe_price_id, active=False, **kwargs)
                needs_new_price = True

        if needs_new_price:
            new_price = stripe.Price.create(
                product=plan.stripe_product_id,
                unit_amount=to_stripe_amount(plan.price, plan.currency),
                currency=plan.currency.lower(),
                recurring={"interval": plan.period_unit, "interval_count": plan.period_count},
                metadata={"revel_plan_id": str(plan.pk)},
                **kwargs,
            )
            plan.stripe_price_id = t.cast(str, new_price.id)
            update_fields.append("stripe_price_id")
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_price_sync_failed",
            plan_id=str(plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Could not sync the plan with Stripe. Please try again later."))) from exc

    if update_fields:
        plan.save(update_fields=[*update_fields, "updated_at"])
    return plan
