"""Stripe integration for membership subscriptions (Phase 2).

Lives next to ``subscription_service`` (the OFFLINE/staff-managed flow) and
adds the ONLINE flow on top of Stripe Connect direct charges. The local
state machine is still authoritative; Stripe events flow back via the
webhook handlers in :mod:`events.service.stripe_webhooks`.
"""

import typing as t
from datetime import datetime
from datetime import timezone as _utc
from decimal import Decimal

import stripe
import structlog
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import (
    CustomerProfile,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service
from events.service.subscription_stripe_dispatch import (
    _dispatch_invoice_notifications,
    _dispatch_sync_notifications,
)
from events.utils.currency import from_stripe_amount, to_stripe_amount

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service):
# this module makes its own outbound calls and must not rely on another
# module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


# ---- Stripe-account helpers --------------------------------------------------


def _stripe_account_kwargs(organization: Organization) -> dict[str, str]:
    """Return ``stripe_account=...`` kwargs for a Connect API call.

    When the organization happens to share the platform's own Stripe account,
    omit the kwarg entirely (mirrors :mod:`events.service.stripe_service`).
    """
    if organization.stripe_account_id and organization.stripe_account_id != settings.STRIPE_ACCOUNT:
        return {"stripe_account": organization.stripe_account_id}
    return {}


def _require_stripe_connected(organization: Organization) -> None:
    """Raise 400 if the organization has not finished Stripe Connect onboarding."""
    if not organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))


# ---- Customer profile --------------------------------------------------------


def ensure_customer_profile(user: RevelUser, organization: Organization) -> CustomerProfile:
    """Return the per-(user, organization) Stripe Customer, creating it if needed."""
    _require_stripe_connected(organization)
    existing = CustomerProfile.objects.filter(user=user, organization=organization).first()
    if existing:
        return existing

    try:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.get_display_name() or None,
            metadata={"revel_user_id": str(user.pk), "revel_org_id": str(organization.pk)},
            # Deterministic key keeps concurrent first-time subscribes from
            # creating duplicate Stripe Customers on the same Connect account.
            idempotency_key=f"cust:{user.pk}:{organization.pk}",
            **_stripe_account_kwargs(organization),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_customer_create_failed",
            user_id=str(user.pk),
            org_id=str(organization.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    profile, _created = get_or_create_with_race_protection(
        CustomerProfile,
        models.Q(user=user, organization=organization),
        {
            "user": user,
            "organization": organization,
            "stripe_customer_id": t.cast(str, customer.id),
        },
    )
    return profile


# ---- Product + Price provisioning -------------------------------------------


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
                name=f"{plan.tier.name} — {plan.name}",
                description=plan.description or None,
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


def archive_stripe_price(plan: MembershipSubscriptionPlan) -> None:
    """Deactivate the Stripe Price for an archived ONLINE plan.

    Existing subscribers keep paying their old Price (Stripe links are by
    subscription, not by the active flag), but the Price can no longer be
    used for new subscriptions.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return
    if not plan.stripe_price_id:
        return
    org = plan.tier.organization
    if not org.is_stripe_connected:
        return
    try:
        stripe.Price.modify(plan.stripe_price_id, active=False, **_stripe_account_kwargs(org))
    except stripe.error.InvalidRequestError as exc:
        # Price already archived or no longer exists — nothing actionable.
        logger.warning(
            "subscription_archive_stripe_price_failed",
            plan_id=str(plan.pk),
            stripe_price_id=plan.stripe_price_id,
            error=str(exc),
        )


# ---- Subscribe / cancel -----------------------------------------------------


def start_online_subscription(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
) -> tuple[MembershipSubscription, str]:
    """Start an ONLINE subscription via Stripe.

    Creates the local row via :func:`subscription_service.create_subscription`
    (re-using its BANNED / duplicate-active checks and member sync), then
    creates a Stripe Subscription with ``payment_behavior=default_incomplete``
    so the client can confirm the first invoice's PaymentIntent.

    Returns:
        A ``(subscription, client_secret)`` pair. The caller hands
        ``client_secret`` to Stripe.js to confirm the first payment.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This plan is not configured for online checkout.")))
    if not plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))

    org = plan.tier.organization
    _require_stripe_connected(org)

    # Lazily provision Stripe Product+Price if missing (e.g. plan was created
    # before Phase 2 or the Stripe call previously failed).
    if not plan.stripe_price_id:
        ensure_stripe_price(plan)
        plan.refresh_from_db()
        if not plan.stripe_price_id:
            raise HttpError(500, str(_("Could not prepare the plan for checkout.")))

    customer = ensure_customer_profile(user, org)

    # Local PENDING row commits before the Stripe call (transaction is outside
    # start_online_subscription to avoid holding a lock across a slow API call).
    subscription = subscription_service.create_subscription(plan, user)

    create_kwargs: dict[str, t.Any] = {
        "customer": customer.stripe_customer_id,
        "items": [{"price": plan.stripe_price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.confirmation_secret"],
        "metadata": {
            "revel_subscription_id": str(subscription.pk),
            "revel_user_id": str(user.pk),
            "revel_org_id": str(org.pk),
            "revel_plan_id": str(plan.pk),
        },
        # Deterministic key tied to the local row makes Stripe-side retries
        # idempotent: a network hiccup that times out the create call won't
        # accidentally provision two subscriptions on the next attempt.
        "idempotency_key": f"sub:{subscription.pk}",
    }
    if org.platform_fee_percent and org.stripe_account_id and org.stripe_account_id != settings.STRIPE_ACCOUNT:
        create_kwargs["application_fee_percent"] = float(org.platform_fee_percent)
    create_kwargs.update(_stripe_account_kwargs(org))

    try:
        stripe_sub = stripe.Subscription.create(**create_kwargs)
    except stripe.error.StripeError as exc:
        # Roll back the local PENDING row so the user can retry cleanly.
        subscription.delete()
        logger.error(
            "subscription_stripe_create_failed",
            plan_id=str(plan.pk),
            user_id=str(user.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    client_secret = _extract_client_secret(stripe_sub)
    if not client_secret:
        # Stripe accepted the create call but didn't return an expandable
        # PaymentIntent. The user can't confirm payment, so cancel the Stripe
        # sub (best-effort) and drop the local row so they aren't blocked by
        # the partial-unique index when they retry.
        logger.warning(
            "subscription_stripe_missing_client_secret",
            stripe_subscription_id=stripe_sub.id,
            subscription_id=str(subscription.pk),
        )
        try:
            stripe.Subscription.cancel(stripe_sub.id, **_stripe_account_kwargs(org))  # type: ignore[attr-defined]
        except stripe.error.StripeError:
            logger.exception(
                "subscription_stripe_cleanup_cancel_failed",
                stripe_subscription_id=stripe_sub.id,
            )
        subscription.delete()
        raise HttpError(502, str(_("Payment processing failed. Please try again later.")))

    subscription.stripe_subscription_id = t.cast(str, stripe_sub.id)
    subscription.save(update_fields=["stripe_subscription_id", "updated_at"])
    return subscription, client_secret


def _extract_client_secret(stripe_sub: stripe.Subscription) -> str | None:
    """Pull the payment client_secret off an expanded Subscription.

    From API version 2025-03-31.basil (we pin dahlia) the confirmable secret
    lives at ``latest_invoice.confirmation_secret.client_secret`` (expanded at
    create time); ``invoice.payment_intent`` no longer exists. The legacy path
    is kept as a fallback for old fixtures / unpinned tooling.
    """
    invoice = getattr(stripe_sub, "latest_invoice", None)
    if not invoice:
        return None

    def _get(obj: t.Any, key: str) -> t.Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    confirmation_secret = _get(invoice, "confirmation_secret")
    if confirmation_secret:
        secret = _get(confirmation_secret, "client_secret")
        if secret:
            return t.cast(str, secret)

    payment_intent = _get(invoice, "payment_intent")
    if not payment_intent:
        return None
    return t.cast(str | None, _get(payment_intent, "client_secret"))


def create_revival_subscription(subscription: MembershipSubscription) -> str:
    """Provision a fresh Stripe Subscription for an EXPIRED local row.

    A cancelled Stripe Subscription cannot be reactivated. We create a new
    one bound to the plan's current ``stripe_price_id`` and overwrite the
    local ``stripe_subscription_id``. The old id is preserved in
    ``historical_membership_subscription`` (simple-history).

    Returns the new Stripe Subscription's ``client_secret``. Raises
    ``HttpError(502)`` if Stripe accepts the create call but doesn't return
    a confirmable PaymentIntent (in which case the dangling Stripe sub is
    best-effort cancelled).
    """
    plan = subscription.plan
    org = subscription.organization
    _require_stripe_connected(org)
    if not plan.stripe_price_id:
        plan = ensure_stripe_price(plan)
        subscription.plan = plan

    customer = ensure_customer_profile(subscription.user, org)

    # Scope the idempotency key to this revival attempt via expired_at so that
    # a future revival (after a new EXPIRED transition with a fresh expired_at)
    # gets a distinct key.
    idempotency_key = (
        f"sub-revival:{subscription.pk}:{int(subscription.expired_at.timestamp())}"
        if subscription.expired_at
        else f"sub-revival:{subscription.pk}"
    )

    create_kwargs: dict[str, t.Any] = {
        "customer": customer.stripe_customer_id,
        "items": [{"price": plan.stripe_price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.confirmation_secret"],
        "metadata": {
            "revel_subscription_id": str(subscription.pk),
            "revel_user_id": str(subscription.user_id),
            "revel_org_id": str(org.pk),
            "revel_plan_id": str(plan.pk),
        },
        "idempotency_key": idempotency_key,
    }
    if org.platform_fee_percent and org.stripe_account_id and org.stripe_account_id != settings.STRIPE_ACCOUNT:
        create_kwargs["application_fee_percent"] = float(org.platform_fee_percent)
    create_kwargs.update(_stripe_account_kwargs(org))

    try:
        stripe_sub = stripe.Subscription.create(**create_kwargs)
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_revival_stripe_create_failed",
            subscription_id=str(subscription.pk),
            plan_id=str(plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    client_secret = _extract_client_secret(stripe_sub)
    if not client_secret:
        # Stripe accepted the create call but didn't return a confirmable
        # PaymentIntent. Cancel the dangling Stripe sub (best-effort) and
        # leave the local row intact — it has meaningful history and should
        # remain EXPIRED so the user can retry.
        logger.warning(
            "subscription_revival_stripe_missing_client_secret",
            stripe_subscription_id=stripe_sub.id,
            subscription_id=str(subscription.pk),
        )
        try:
            stripe.Subscription.cancel(stripe_sub.id, **_stripe_account_kwargs(org))  # type: ignore[attr-defined]
        except stripe.error.StripeError:
            logger.exception(
                "subscription_revival_cleanup_cancel_failed",
                stripe_subscription_id=stripe_sub.id,
                subscription_id=str(subscription.pk),
            )
        raise HttpError(502, str(_("Could not initialize the payment. Please try again later.")))

    subscription.stripe_subscription_id = t.cast(str, stripe_sub.id)
    subscription.status = MembershipSubscription.SubscriptionStatus.PENDING
    # Reset period — Stripe populates it via the first invoice.paid webhook.
    subscription.current_period_start = None
    subscription.current_period_end = None
    subscription.save(
        update_fields=[
            "stripe_subscription_id",
            "status",
            "current_period_start",
            "current_period_end",
            "updated_at",
        ]
    )
    return client_secret


def cancel_online_subscription(
    subscription: MembershipSubscription,
    *,
    immediate: bool = False,
) -> MembershipSubscription:
    """Cancel an ONLINE subscription on Stripe.

    Local state is mirrored via the ``customer.subscription.updated`` /
    ``customer.subscription.deleted`` webhooks. As a UX nicety we also reflect
    the scheduled flag locally right away so the API caller sees an immediate
    response without waiting for the webhook round-trip.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
    if subscription.is_terminal:
        return subscription

    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)

    if immediate:
        # ``Subscription.cancel`` is the documented runtime API; the type stubs
        # don't expose it as a classmethod, hence the ignore.
        stripe.Subscription.cancel(subscription.stripe_subscription_id, **kwargs)  # type: ignore[attr-defined]
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
    else:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            cancel_at_period_end=True,
            **kwargs,
        )
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
    return subscription


# ---- Plan changes (Phase 3) -------------------------------------------------
# Full implementation lives in subscription_stripe_plan_change.py.
# See subscription_service.change_plan for the dispatch entry-point.


def update_subscription_price(subscription: MembershipSubscription) -> bool:
    """Swap the Stripe Price to the plan's current price (proration_behavior='none').

    Returns True when modify was called; False when already current or skipped.
    """
    if not subscription.stripe_subscription_id:
        return False
    current_price_id = subscription.plan.stripe_price_id
    if not current_price_id:
        return False

    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)
    try:
        stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id, **kwargs)
    except stripe.error.StripeError as exc:
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    items = (stripe_sub.get("items") or {}).get("data") or []
    if not items:
        return False
    item = items[0]
    if (item.get("price") or {}).get("id") == current_price_id:
        return False
    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            items=[{"id": item["id"], "price": current_price_id}],
            proration_behavior="none",
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_price_swap_failed",
            subscription_id=str(subscription.pk),
            target_price_id=current_price_id,
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    return True


def pause_online_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Pause invoice collection on Stripe.

    Uses ``pause_collection.behavior='void'`` so any draft invoices created
    while paused are voided rather than sitting around. The subscription
    keeps its existing ``status`` on Stripe (``active``); we surface PAUSED
    locally so members and staff see a clear signal.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot pause a terminal subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        return subscription

    org = subscription.organization
    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            pause_collection={"behavior": "void"},
            **_stripe_account_kwargs(org),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_pause_failed",
            subscription_id=str(subscription.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def resume_online_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Resume a previously paused Stripe subscription.

    Sending ``pause_collection=""`` clears the pause on Stripe; the local
    status flips back to ACTIVE. The grace-expiry Celery task will move it
    to PAST_DUE later if the period has already lapsed during the pause.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
    if subscription.status != MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Only paused subscriptions can be resumed.")))

    org = subscription.organization
    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            pause_collection="",
            **_stripe_account_kwargs(org),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_resume_failed",
            subscription_id=str(subscription.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


# ---- Customer Portal --------------------------------------------------------


def create_billing_portal_session(
    user: RevelUser,
    organization: Organization,
    *,
    return_url: str,
) -> str:
    """Return a URL to a Stripe Customer Portal session for ``user`` in ``organization``.

    The Customer Portal lets members manage their saved payment methods,
    view invoices, and (if enabled in the Stripe dashboard) cancel/change
    their subscription. Requires an existing per-(user, org) Stripe Customer
    — only users who have actually subscribed can get a portal session.
    This keeps strangers from triggering Stripe Customer creation on
    arbitrary Connect accounts via the public endpoint.
    """
    _require_stripe_connected(organization)
    customer = CustomerProfile.objects.filter(user=user, organization=organization).first()
    if customer is None:
        raise HttpError(404, str(_("No billing profile exists for this organization. Subscribe first.")))
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer.stripe_customer_id,
            return_url=return_url,
            **_stripe_account_kwargs(organization),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_billing_portal_failed",
            user_id=str(user.pk),
            org_id=str(organization.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    return t.cast(str, session.url)


# ---- Webhook helpers --------------------------------------------------------


def _ensure_active_member(subscription: MembershipSubscription) -> None:
    """Make sure an :class:`OrganizationMember` exists for an ONLINE subscriber.

    Phase 1's signal-driven sync intentionally never *creates* members; that
    responsibility belongs to :func:`subscription_service.create_subscription`
    for the OFFLINE flow. For ONLINE plans, the equivalent moment is the
    first successful invoice / Stripe ``active`` status — both of which land
    in this module's webhook helpers. We use ``get_or_create`` so that an
    existing BANNED row stays BANNED (the post-save signal then preserves
    that state when it fires).
    """
    OrganizationMember.objects.get_or_create(
        organization=subscription.organization,
        user=subscription.user,
        defaults={
            "tier": subscription.plan.tier,
            "status": OrganizationMember.MembershipStatus.ACTIVE,
        },
    )


_STRIPE_STATUS_MAP: dict[str, str] = {
    "incomplete": MembershipSubscription.SubscriptionStatus.PENDING.value,
    "incomplete_expired": MembershipSubscription.SubscriptionStatus.EXPIRED.value,
    "trialing": MembershipSubscription.SubscriptionStatus.ACTIVE.value,
    "active": MembershipSubscription.SubscriptionStatus.ACTIVE.value,
    "past_due": MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    "unpaid": MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    "canceled": MembershipSubscription.SubscriptionStatus.CANCELLED.value,
    "paused": MembershipSubscription.SubscriptionStatus.PAUSED.value,
}


def map_stripe_status(stripe_status: str) -> str | None:
    """Translate a Stripe ``Subscription.status`` to our local enum value."""
    return _STRIPE_STATUS_MAP.get(stripe_status)


def _epoch_to_dt(epoch: int | None) -> datetime | None:
    """Convert a Stripe Unix timestamp to a tz-aware datetime."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=_utc.utc)


def _resolve_target_status(stripe_subscription: dict[str, t.Any]) -> str | None:
    """Translate the Stripe payload to a local status, honoring ``pause_collection``.

    Stripe surfaces an active pause via the ``pause_collection`` object, not
    via the top-level ``status`` field. When set, we treat the subscription
    as PAUSED locally — *unless* the mapped Stripe status is terminal
    (CANCELLED/EXPIRED). Terminal wins so a deletion event that still carries
    a stale ``pause_collection`` doesn't un-terminalize the local row and
    re-arm the one-active-subscription-per-(user, org) unique index.
    """
    mapped = map_stripe_status(t.cast(str, stripe_subscription.get("status", "")))
    if mapped in MembershipSubscription.TERMINAL_STATUSES:
        return mapped
    if stripe_subscription.get("pause_collection"):
        return MembershipSubscription.SubscriptionStatus.PAUSED.value
    return mapped


def _subscription_period_epochs(stripe_subscription: dict[str, t.Any]) -> tuple[int | None, int | None]:
    """Extract ``current_period_{start,end}`` from a Subscription payload.

    API versions >= 2025-03-31.basil (we pin dahlia) moved the period from the
    Subscription's top level onto each subscription item; single-item
    subscriptions (our only shape) carry it on ``items.data[0]``. Older
    payloads (tests, fixtures, any unpinned tooling) still have the top-level
    fields, so fall back to those.
    """
    items_data = (stripe_subscription.get("items") or {}).get("data") or []
    item = items_data[0] if items_data else {}
    start = item.get("current_period_start") or stripe_subscription.get("current_period_start")
    end = item.get("current_period_end") or stripe_subscription.get("current_period_end")
    return start, end


def _apply_period_dates(
    subscription: MembershipSubscription,
    stripe_subscription: dict[str, t.Any],
) -> list[str]:
    """Mirror Stripe's ``current_period_*`` epochs onto the local row in place."""
    changed: list[str] = []
    start_epoch, end_epoch = _subscription_period_epochs(stripe_subscription)
    new_start = _epoch_to_dt(start_epoch)
    if new_start and subscription.current_period_start != new_start:
        subscription.current_period_start = new_start
        changed.append("current_period_start")
    new_end = _epoch_to_dt(end_epoch)
    if new_end and subscription.current_period_end != new_end:
        subscription.current_period_end = new_end
        changed.append("current_period_end")
    return changed


def _apply_stripe_price_swap(
    subscription: MembershipSubscription,
    stripe_subscription: dict[str, t.Any],
) -> list[str]:
    """Detect a Stripe price swap and re-point ``subscription.plan`` if needed.

    Returns the list of field names that were mutated on ``subscription`` so
    the caller can extend its own ``update_fields`` list. Mutates the
    instance in place but does not save. Terminal rows are frozen — late
    webhook events for a cancelled/expired subscription must not rewrite
    the historical plan FK.
    """
    if subscription.is_terminal:
        return []
    items = (stripe_subscription.get("items") or {}).get("data") or []
    active_price_id = (items[0].get("price", {}).get("id") if items else None) or None
    if not active_price_id or active_price_id == subscription.plan.stripe_price_id:
        return []
    new_plan = MembershipSubscriptionPlan.objects.filter(
        stripe_price_id=active_price_id,
        tier__organization=subscription.organization,
    ).first()
    if not new_plan or new_plan.pk == subscription.plan_id:
        return []
    changed = ["plan"]
    subscription.plan = new_plan
    if subscription.pending_plan_id == new_plan.pk:
        subscription.pending_plan = None
        changed.append("pending_plan")
    if subscription.stripe_schedule_id:
        subscription.stripe_schedule_id = ""
        changed.append("stripe_schedule_id")
    return changed


@transaction.atomic
def sync_subscription_from_stripe(
    stripe_subscription: dict[str, t.Any],
) -> MembershipSubscription | None:
    """Mirror Stripe Subscription state onto our local row.

    Used by ``customer.subscription.{created,updated,deleted}`` handlers.
    Returns ``None`` if we don't know the subscription locally — Stripe-side
    rows for unrelated Connect accounts are expected and silently ignored.
    """
    stripe_id = stripe_subscription.get("id")
    if not stripe_id:
        return None
    subscription = (
        MembershipSubscription.objects.select_for_update()
        .select_related("plan", "plan__tier", "organization", "user")
        .filter(stripe_subscription_id=stripe_id)
        .first()
    )
    if subscription is None:
        return None

    prior_status = subscription.status  # captured before mutations for D3 dispatch gates
    prior_cap = subscription.cancel_at_period_end

    update_fields: list[str] = []

    target_status = _resolve_target_status(stripe_subscription)
    if target_status and subscription.status != target_status:
        subscription.status = target_status
        update_fields.append("status")
        if target_status == MembershipSubscription.SubscriptionStatus.CANCELLED.value and not subscription.cancelled_at:
            subscription.cancelled_at = timezone.now()
            update_fields.append("cancelled_at")
        if target_status == MembershipSubscription.SubscriptionStatus.EXPIRED.value and not subscription.expired_at:
            subscription.expired_at = timezone.now()
            update_fields.append("expired_at")

    cap = bool(stripe_subscription.get("cancel_at_period_end", False))
    if subscription.cancel_at_period_end != cap:
        subscription.cancel_at_period_end = cap
        update_fields.append("cancel_at_period_end")

    update_fields.extend(_apply_period_dates(subscription, stripe_subscription))
    # Detect a price swap (schedule phase transition or direct upgrade) and
    # re-point ``subscription.plan`` accordingly.
    update_fields.extend(_apply_stripe_price_swap(subscription, stripe_subscription))

    if update_fields:
        subscription.save(update_fields=[*update_fields, "updated_at"])
    if subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE.value:
        _ensure_active_member(subscription)
    _dispatch_sync_notifications(subscription, prior_status=prior_status, prior_cap=prior_cap)
    return subscription


def _as_stripe_id(value: t.Any) -> str:
    """Normalize a possibly-expanded Stripe reference to its string id."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return t.cast(str, value.get("id") or "")
    return t.cast(str, getattr(value, "id", "") or "")


def _invoice_subscription_id(invoice: dict[str, t.Any]) -> str:
    """Extract the Subscription id from an Invoice payload.

    API versions >= 2025-03-31.basil (we pin dahlia) moved it from the
    top-level ``subscription`` field to
    ``parent.subscription_details.subscription``. Try the modern path first,
    then the legacy field (old fixtures / unpinned tooling).
    """
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    modern = _as_stripe_id(details.get("subscription"))
    if modern:
        return modern
    return _as_stripe_id(invoice.get("subscription"))


def _invoice_payment_intent_id(invoice: dict[str, t.Any], organization: Organization) -> str:
    """Extract the PaymentIntent id from an Invoice payload, fetching if needed.

    Pre-basil payloads carry ``invoice.payment_intent``. From 2025-03-31.basil
    an invoice can have multiple partial payments and the field moved to the
    ``payments`` list (``payments.data[].payment.payment_intent``), which
    webhook payloads do NOT embed — so fall back to an outbound
    ``stripe.Invoice.retrieve(..., expand=["payments"])``. Best-effort: the id
    feeds refund routing (charge.refunded → MembershipPayment matching) and
    audit, so an empty string is tolerated rather than failing the webhook.
    """
    legacy = _as_stripe_id(invoice.get("payment_intent"))
    if legacy:
        return legacy

    def _scan(payments_obj: t.Any) -> str:
        data = (payments_obj or {}).get("data") or []
        for entry in data:
            intent = _as_stripe_id((entry.get("payment") or {}).get("payment_intent"))
            if intent:
                return intent
        return ""

    found = _scan(invoice.get("payments"))
    if found:
        return found

    invoice_id = invoice.get("id")
    if not invoice_id:
        return ""
    try:
        retrieved = stripe.Invoice.retrieve(
            invoice_id,
            expand=["payments"],
            **_stripe_account_kwargs(organization),
        )
    except stripe.error.StripeError:
        logger.warning("subscription_invoice_payments_fetch_failed", stripe_invoice_id=invoice_id)
        return ""
    return _scan(dict(retrieved).get("payments"))


@transaction.atomic
def record_stripe_payment_from_invoice(
    invoice: dict[str, t.Any],
    *,
    succeeded: bool,
) -> MembershipPayment | None:
    """Create or update a :class:`MembershipPayment` from a Stripe Invoice event.

    Args:
        invoice: The Stripe ``invoice.*`` event's ``data.object``.
        succeeded: ``True`` for ``invoice.paid``; ``False`` for
            ``invoice.payment_failed``.

    Returns:
        The created/updated :class:`MembershipPayment`, or ``None`` when the
        invoice belongs to a Stripe Subscription we don't know.
    """
    stripe_sub_id = _invoice_subscription_id(invoice)
    invoice_id = invoice.get("id")
    if not stripe_sub_id or not invoice_id:
        return None

    subscription = (
        MembershipSubscription.objects.select_for_update()
        .select_related("plan", "plan__tier", "organization")
        .filter(stripe_subscription_id=stripe_sub_id)
        .first()
    )
    if subscription is None:
        return None

    prior_status = subscription.status  # captured before mutations for D3 dispatch gates

    currency_code = t.cast(str, invoice.get("currency") or subscription.plan.currency).upper()
    # SUCCEEDED: amount_paid (what changed hands). FAILED: 0, raw_response keeps the attempted amount.
    if succeeded:
        amount_minor = int(invoice.get("amount_paid") or 0)
    else:
        amount_minor = 0
    amount = from_stripe_amount(amount_minor, currency_code) if amount_minor else Decimal("0")

    lines_data = (invoice.get("lines") or {}).get("data") or []
    period = (lines_data[0].get("period") if lines_data else None) or {}
    period_start = _epoch_to_dt(period.get("start")) or timezone.now()
    period_end = _epoch_to_dt(period.get("end")) or timezone.now()

    payment_intent_id = _invoice_payment_intent_id(invoice, subscription.organization)

    payment, created = MembershipPayment.objects.update_or_create(
        stripe_invoice_id=invoice_id,
        defaults={
            "subscription": subscription,
            "amount": amount,
            "currency": currency_code,
            "status": (
                MembershipPayment.PaymentStatus.SUCCEEDED if succeeded else MembershipPayment.PaymentStatus.FAILED
            ),
            "period_start": period_start,
            "period_end": period_end,
            "stripe_payment_intent_id": payment_intent_id,
            "raw_response": invoice,
        },
    )

    if succeeded:
        # Mirror the period from the invoice line and revive PENDING/PAST_DUE.
        update_fields: list[str] = []
        if subscription.current_period_start != period_start:
            subscription.current_period_start = period_start
            update_fields.append("current_period_start")
        if subscription.current_period_end != period_end:
            subscription.current_period_end = period_end
            update_fields.append("current_period_end")
        revivable = {
            MembershipSubscription.SubscriptionStatus.PENDING.value,
            MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
        }
        if subscription.status in revivable:
            subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
            update_fields.append("status")
        if update_fields:
            subscription.save(update_fields=[*update_fields, "updated_at"])
        _ensure_active_member(subscription)
    else:
        # Mirror PAST_DUE; the grace-expiry Celery task takes over from here.
        if subscription.status in {
            MembershipSubscription.SubscriptionStatus.ACTIVE.value,
            MembershipSubscription.SubscriptionStatus.PENDING.value,
        }:
            subscription.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
            subscription.save(update_fields=["status", "updated_at"])

    _dispatch_invoice_notifications(
        subscription, prior_status=prior_status, succeeded=succeeded, payment_created=created
    )

    logger.info(
        "subscription_stripe_payment_recorded",
        subscription_id=str(subscription.pk),
        stripe_invoice_id=invoice_id,
        succeeded=succeeded,
        amount=str(amount),
        currency=currency_code,
        created=created,
    )
    return payment
