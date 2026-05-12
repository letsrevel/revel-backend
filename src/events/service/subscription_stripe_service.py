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
from events.utils.currency import from_stripe_amount, to_stripe_amount

logger = structlog.get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


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

    # ``create_subscription`` is already ``@transaction.atomic`` — no outer
    # wrapper needed. The local PENDING row commits before the Stripe call,
    # which is intentional: holding a DB transaction open across a slow
    # external API call would lock the row for the entire request.
    subscription = subscription_service.create_subscription(plan, user)

    create_kwargs: dict[str, t.Any] = {
        "customer": customer.stripe_customer_id,
        "items": [{"price": plan.stripe_price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.payment_intent"],
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
    """Pull the PaymentIntent client_secret off an expanded Subscription."""
    invoice = getattr(stripe_sub, "latest_invoice", None)
    if not invoice:
        return None
    payment_intent = (
        invoice.get("payment_intent") if isinstance(invoice, dict) else getattr(invoice, "payment_intent", None)
    )
    if not payment_intent:
        return None
    if isinstance(payment_intent, dict):
        return t.cast(str | None, payment_intent.get("client_secret"))
    return t.cast(str | None, getattr(payment_intent, "client_secret", None))


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

    update_fields: list[str] = []

    mapped = map_stripe_status(t.cast(str, stripe_subscription.get("status", "")))
    if mapped and subscription.status != mapped:
        subscription.status = mapped
        update_fields.append("status")
        if mapped == MembershipSubscription.SubscriptionStatus.CANCELLED.value and not subscription.cancelled_at:
            subscription.cancelled_at = timezone.now()
            update_fields.append("cancelled_at")

    cap = bool(stripe_subscription.get("cancel_at_period_end", False))
    if subscription.cancel_at_period_end != cap:
        subscription.cancel_at_period_end = cap
        update_fields.append("cancel_at_period_end")

    new_start = _epoch_to_dt(stripe_subscription.get("current_period_start"))
    if new_start and subscription.current_period_start != new_start:
        subscription.current_period_start = new_start
        update_fields.append("current_period_start")
    new_end = _epoch_to_dt(stripe_subscription.get("current_period_end"))
    if new_end and subscription.current_period_end != new_end:
        subscription.current_period_end = new_end
        update_fields.append("current_period_end")

    if update_fields:
        subscription.save(update_fields=[*update_fields, "updated_at"])
    if subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE.value:
        _ensure_active_member(subscription)
    return subscription


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
    stripe_sub_id = invoice.get("subscription")
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

    currency_code = t.cast(str, invoice.get("currency") or subscription.plan.currency).upper()
    # FAILED payments collected nothing — store ``amount=0``. The attempted
    # amount is preserved in ``raw_response``. SUCCEEDED payments use
    # ``amount_paid`` (what actually changed hands).
    if succeeded:
        amount_minor = int(invoice.get("amount_paid") or 0)
    else:
        amount_minor = 0
    amount = from_stripe_amount(amount_minor, currency_code) if amount_minor else Decimal("0")

    period = invoice.get("lines", {}).get("data", [{}])[0].get("period") or {}
    period_start = _epoch_to_dt(period.get("start")) or timezone.now()
    period_end = _epoch_to_dt(period.get("end")) or timezone.now()

    payment_intent_id = invoice.get("payment_intent") or ""

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
        # Mirror PAST_DUE for failed payments. The grace-expiry Celery task
        # from Phase 1 takes over from here.
        if subscription.status in {
            MembershipSubscription.SubscriptionStatus.ACTIVE.value,
            MembershipSubscription.SubscriptionStatus.PENDING.value,
        }:
            subscription.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
            subscription.save(update_fields=["status", "updated_at"])

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
