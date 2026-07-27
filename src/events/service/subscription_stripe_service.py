"""Stripe integration for membership subscriptions (Phase 2).

Lives next to ``subscription_service`` (the OFFLINE/staff-managed flow) and
adds the ONLINE flow on top of Stripe Connect direct charges. The local
state machine is still authoritative; Stripe events flow back via the
webhook handlers in :mod:`events.service.stripe_webhooks`.
"""

import typing as t
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
import structlog
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.service.vat_utils import b2b_vat_context
from common.utils import get_or_create_with_race_protection
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service import subscription_sales, subscription_service
from events.service.subscription_stripe_payloads import (
    _stripe_account_kwargs,
)
from events.utils.currency import to_stripe_amount

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service):
# this module makes its own outbound calls and must not rely on another
# module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


# ---- Stripe-account helpers --------------------------------------------------


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


def _checkout_session_urls(organization: Organization) -> dict[str, str]:
    """Success/cancel redirect URLs for a membership Checkout Session.

    Mirrors the ticket checkout convention (``stripe_service._create_stripe_session``):
    redirect back to the org page with a query flag the frontend reads.
    """
    from common.models import SiteSettings  # lazy: avoid import cycle

    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    return {
        "success_url": f"{frontend_base_url}/org/{organization.slug}?membership_success=true",
        "cancel_url": f"{frontend_base_url}/org/{organization.slug}?membership_cancelled=true",
    }


def _effective_application_fee_percent(org: Organization) -> Decimal | None:
    """Org fee percent grossed up with platform VAT when applicable.

    Tickets charge the org fee + VAT on the fee (``calculate_platform_fee_vat``
    adds VAT on top). ``application_fee_percent`` is the only fee mechanism for
    subscriptions (Stripe has no fixed/absolute variant), so the same economics
    are achieved by grossing the percent itself up. The fixed fee component
    (``org.platform_fee_fixed``) intentionally does NOT apply to subscriptions.

    Returns:
        The percent to send to Stripe, or ``None`` when no fee applies.
    """
    from common.models import SiteSettings  # lazy: avoid import cycle

    if not org.platform_fee_percent:
        return None
    if not org.stripe_account_id or org.stripe_account_id == settings.STRIPE_ACCOUNT:
        return None
    site = SiteSettings.get_solo()
    reverse_charge, rate = b2b_vat_context(org, site.platform_vat_country, site.platform_vat_rate)
    if reverse_charge or rate <= 0:
        return org.platform_fee_percent
    grossed = (org.platform_fee_percent * (1 + rate / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Stripe rejects percents above 100; the org field is capped at 100, so a
    # gross-up can theoretically overshoot.
    return min(grossed, Decimal("100"))


def _create_subscription_checkout_session(
    subscription: MembershipSubscription,
    customer: CustomerProfile,
    *,
    idempotency_key: str,
) -> stripe.checkout.Session:
    """Create a hosted Checkout Session (``mode=subscription``) for a local row.

    ``metadata.membership_subscription_id`` (on both the session and the
    Stripe Subscription it will create) is the join key the
    ``checkout.session.completed`` handler uses to link
    ``stripe_subscription_id`` back onto the local row.

    Raises:
        stripe.error.StripeError: Propagated from the Stripe API call; the
            caller owns local-row cleanup.
    """
    plan = subscription.plan
    org = subscription.organization
    metadata = {"membership_subscription_id": str(subscription.pk)}
    subscription_data: dict[str, t.Any] = {"metadata": metadata}
    effective_percent = _effective_application_fee_percent(org)
    if effective_percent is not None:
        subscription_data["application_fee_percent"] = float(effective_percent)
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.stripe_customer_id,
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        subscription_data=subscription_data,
        metadata=metadata,
        expires_at=int(expires_at.timestamp()),
        # Deterministic key tied to the local row makes Stripe-side retries
        # idempotent: a network hiccup that times out the create call won't
        # accidentally mint two sessions on the next attempt.
        idempotency_key=idempotency_key,
        **_checkout_session_urls(org),
        **_stripe_account_kwargs(org),
    )


def start_online_subscription(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
) -> tuple[MembershipSubscription, str]:
    """Start an ONLINE subscription via hosted Stripe Checkout.

    Creates the local row via :func:`subscription_service.create_subscription`
    (re-using its BANNED / duplicate-active checks and member sync), then
    creates a Checkout Session (``mode=subscription``) on the org's Connect
    account. The Stripe Subscription itself is only created when the member
    completes the session; ``checkout.session.completed`` links its id back
    onto the local row.

    Returns:
        A ``(subscription, checkout_url)`` pair. The caller redirects the
        member to ``checkout_url``.

    NOTE: for plans with ``max_subscriptions`` set, ``create_subscription``
    takes the plan row lock (``ensure_plan_sales_capacity``); under production
    ATOMIC_REQUESTS the inner atomic exit releases only a savepoint, so that
    lock is held across the Checkout-Session Stripe call below until the
    request commits — serializing concurrent subscribers to a capped plan.
    Accepted for now (see engineering-notes, "reserve/session split"): capped
    plans are the exception and don't see on-sale-rush traffic; the split is
    the upgrade path if one ever does.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This plan is not configured for online checkout.")))
    if not plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))
    # Member self-service path: a PAUSED plan sells to no one (staff-created
    # OFFLINE subs don't come through here). The capacity cap is enforced
    # race-safely inside create_subscription, under the plan row lock.
    subscription_sales.ensure_plan_on_sale(plan)

    org = plan.tier.organization
    _require_stripe_connected(org)

    # Abandoned-checkout recovery: a PENDING row from an abandoned redirect
    # would otherwise trip create_subscription's duplicate-active check and
    # lock the user out until the session expires on Stripe's side.
    resumed = _maybe_resume_pending_checkout(plan, user)
    if resumed is not None:
        return resumed

    # Lazily provision Stripe Product+Price if missing (e.g. plan was created
    # before Phase 2 or the Stripe call previously failed).
    if not plan.stripe_price_id:
        ensure_stripe_price(plan)
        plan.refresh_from_db()
        if not plan.stripe_price_id:
            raise HttpError(500, str(_("Could not prepare the plan for checkout.")))

    customer = ensure_customer_profile(user, org)

    # Local PENDING row is created before the Stripe call. NOTE: under
    # production ATOMIC_REQUESTS the row is NOT yet visible to other
    # transactions during the Stripe round-trip (the whole request is one
    # transaction) — a ``checkout.session.completed`` webhook cannot race us
    # here because the member can only reach the session via the URL this
    # request returns after commit.
    subscription = subscription_service.create_subscription(plan, user)

    try:
        session = _create_subscription_checkout_session(
            subscription,
            customer,
            idempotency_key=f"sub-checkout:{subscription.pk}",
        )
    except stripe.error.StripeError as exc:
        # Roll back the local PENDING row so the user can retry cleanly.
        subscription.delete()
        logger.error(
            "subscription_stripe_checkout_create_failed",
            plan_id=str(plan.pk),
            user_id=str(user.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    if not session.url:
        # An open session always carries a URL; a missing one means the member
        # cannot pay. Drop the local row so the partial-unique index doesn't
        # block a retry.
        logger.warning(
            "subscription_stripe_checkout_missing_url",
            stripe_checkout_session_id=session.id,
            subscription_id=str(subscription.pk),
        )
        subscription.delete()
        raise HttpError(502, str(_("Payment processing failed. Please try again later.")))

    subscription.stripe_checkout_session_id = t.cast(str, session.id)
    subscription.save(update_fields=["stripe_checkout_session_id", "updated_at"])
    return subscription, t.cast(str, session.url)


def _maybe_resume_pending_checkout(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
) -> tuple[MembershipSubscription, str] | None:
    """Resume the user's abandoned ONLINE checkout, or clear the stale PENDING row.

    Returns ``(subscription, checkout_url)`` when the existing Checkout
    Session is still ``open`` for the same plan, so the frontend can redirect
    the member back to it. Returns ``None`` after clearing a stale/superseded
    row (expiring its session best-effort), letting the caller mint a fresh
    session.

    Raises the duplicate-active 400 when the session already completed or the
    Stripe Subscription is already linked (payment confirmed but the
    activation webhooks haven't landed yet) — creating a second subscription
    there would double-charge.
    """
    # Locked read: two concurrent subscribes (double-submit, stale-tab retry
    # with a different plan) must not diverge — one expiring the session while
    # the other hands its URL back (a dead Checkout page). The lock is
    # per-member (single-member blast radius) and, under ATOMIC_REQUESTS, is
    # held across the Session.retrieve below — same accepted trade as the
    # revive path (see #702's reservation-lock precedent for tickets).
    pending = (
        MembershipSubscription.objects.select_for_update()
        .filter(
            organization=plan.tier.organization,
            user=user,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        .select_related("plan", "organization")
        .first()
    )
    if pending is None:
        return None

    if pending.stripe_subscription_id:
        # A PENDING row only gets its Stripe Subscription id when its session
        # completed — payment is done, activation webhooks are in flight.
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    if not pending.stripe_checkout_session_id:
        # Stranded local row from a failed session-create call: clear and start over.
        _clear_stale_pending_checkout(pending)
        return None

    try:
        session = stripe.checkout.Session.retrieve(
            pending.stripe_checkout_session_id,
            **_stripe_account_kwargs(pending.organization),
        )
    except stripe.error.StripeError:
        logger.exception(
            "subscription_stripe_pending_session_retrieve_failed",
            subscription_id=str(pending.pk),
            stripe_checkout_session_id=pending.stripe_checkout_session_id,
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later.")))

    session_status = t.cast(str, session.status or "")
    if session_status == "complete":
        # Paid on Stripe, webhooks still in flight — never create a second sub.
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    if session_status == "open" and pending.plan_id == plan.pk and session.url:
        logger.info(
            "subscription_stripe_pending_checkout_resumed",
            subscription_id=str(pending.pk),
            stripe_checkout_session_id=pending.stripe_checkout_session_id,
        )
        return pending, t.cast(str, session.url)

    # Different plan, or the session expired: expire the old session
    # (best-effort) and clear the local row.
    if session_status == "open":
        try:
            stripe.checkout.Session.expire(
                pending.stripe_checkout_session_id,
                **_stripe_account_kwargs(pending.organization),
            )
        except stripe.error.StripeError:
            logger.exception(
                "subscription_stripe_pending_session_expire_failed",
                subscription_id=str(pending.pk),
                stripe_checkout_session_id=pending.stripe_checkout_session_id,
            )
    _clear_stale_pending_checkout(pending)
    return None


def _clear_stale_pending_checkout(pending: MembershipSubscription) -> None:
    """Remove a superseded PENDING row so a fresh subscription can be created.

    A pristine row (no payment history) is deleted outright. A revival row
    carries the member's ledger (``MembershipPayment`` cascades on delete), so
    it is reverted to EXPIRED instead — its ``expired_at`` is still set from
    the original expiry, keeping the revival window intact.
    """
    if not pending.payments.exists():
        pending.delete()
        return
    pending.status = MembershipSubscription.SubscriptionStatus.EXPIRED
    pending.stripe_checkout_session_id = ""
    pending.save(update_fields=["status", "stripe_checkout_session_id", "updated_at"])


def cancel_stripe_subscription_best_effort(subscription: MembershipSubscription, *, reason: str) -> bool:
    """Best-effort ``stripe.Subscription.cancel`` for a locally-terminalized row.

    Local terminalization (grace expiry, revival superseding an old sub) must
    close the Stripe side too, or Smart Retries keep dunning a member who has
    already lost access locally (C1/C2 in the 2026-06-10 reassessment). Errors
    are logged, never raised: the local state machine stays authoritative and
    the nightly reconciliation re-observes whatever Stripe ends up with.

    Returns True when the cancel call succeeded (False on no-op or failure).
    """
    if not subscription.stripe_subscription_id:
        return False
    try:
        stripe.Subscription.cancel(  # type: ignore[attr-defined]
            subscription.stripe_subscription_id,
            **_stripe_account_kwargs(subscription.organization),
        )
    except stripe.error.StripeError:
        logger.exception(
            "subscription_stripe_cancel_on_terminalize_failed",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=subscription.stripe_subscription_id,
            reason=reason,
        )
        return False
    logger.info(
        "subscription_stripe_cancelled_on_terminalize",
        subscription_id=str(subscription.pk),
        stripe_subscription_id=subscription.stripe_subscription_id,
        reason=reason,
    )
    return True


def create_revival_checkout(subscription: MembershipSubscription) -> str:
    """Start a hosted Checkout for a fresh Stripe Subscription on an EXPIRED row.

    A cancelled Stripe Subscription cannot be reactivated. We mint a Checkout
    Session (``mode=subscription``) bound to the plan's current
    ``stripe_price_id``; ``checkout.session.completed`` links the new Stripe
    Subscription's id onto this row. The old id is preserved in
    ``historical_membership_subscription`` (simple-history).

    The row transitions to PENDING with its old ``stripe_subscription_id``
    cleared: the sync/reconcile paths must neither freeze it as terminal nor
    re-observe the dead Stripe sub while the checkout is open. An abandoned
    revival checkout is recovered by ``_maybe_resume_pending_checkout`` on the
    next subscribe call (resumed while open, reverted to EXPIRED otherwise).

    Returns the Checkout Session's URL.
    """
    plan = subscription.plan
    org = subscription.organization
    _require_stripe_connected(org)

    # The old Stripe sub may still be alive in past_due dunning when the local
    # row expired first (grace clock beat Smart Retries). Close it before its
    # id is cleared below, or a late retry success would bill a sub whose
    # events no longer match any local row (invisible double billing, C2).
    cancel_stripe_subscription_best_effort(subscription, reason="revival_supersedes")

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

    try:
        session = _create_subscription_checkout_session(subscription, customer, idempotency_key=idempotency_key)
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_revival_checkout_create_failed",
            subscription_id=str(subscription.pk),
            plan_id=str(plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    if not session.url:
        # Leave the local row intact — it has meaningful history and should
        # remain EXPIRED so the user can retry.
        logger.warning(
            "subscription_revival_checkout_missing_url",
            stripe_checkout_session_id=session.id,
            subscription_id=str(subscription.pk),
        )
        raise HttpError(502, str(_("Could not initialize the payment. Please try again later.")))

    subscription.stripe_subscription_id = None
    subscription.stripe_checkout_session_id = t.cast(str, session.id)
    subscription.status = MembershipSubscription.SubscriptionStatus.PENDING
    # Reset period — Stripe populates it via the first invoice.paid webhook.
    subscription.current_period_start = None
    subscription.current_period_end = None
    subscription.save(
        update_fields=[
            "stripe_subscription_id",
            "stripe_checkout_session_id",
            "status",
            "current_period_start",
            "current_period_end",
            "updated_at",
        ]
    )
    return t.cast(str, session.url)


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

    # A pending downgrade makes the subscription schedule-managed on Stripe,
    # which rejects a plain cancel/modify. Release the schedule first (clears
    # stripe_schedule_id + pending_plan locally) so the cancel can proceed.
    from events.service.subscription_stripe_plan_change import release_online_schedule  # lazy: avoid cycle

    release_online_schedule(subscription)

    if immediate:
        try:
            # ``Subscription.cancel`` is the documented runtime API; the type stubs
            # don't expose it as a classmethod, hence the ignore.
            stripe.Subscription.cancel(subscription.stripe_subscription_id, **kwargs)  # type: ignore[attr-defined]
        except stripe.error.InvalidRequestError:
            # Already canceled/gone on Stripe (e.g. staff canceled in the Dashboard
            # and this call races the ``customer.subscription.deleted`` webhook, or
            # runs inside the ``charge.refunded`` auto-cancel). The caller's intent
            # is "make it canceled" — proceed with local terminalization; raising
            # here would 500 the request/webhook and (in the webhook case) roll
            # back the dedup row into a permanent Stripe retry loop.
            logger.info(
                "subscription_stripe_cancel_already_terminal",
                subscription_id=str(subscription.pk),
                stripe_subscription_id=subscription.stripe_subscription_id,
            )
        except stripe.error.StripeError as exc:
            # Transient failure (network, rate limit): surface the module's
            # normal retryable 502 instead of a bare 500, and leave local
            # state untouched so a retry starts clean.
            raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
    else:
        try:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=True,
                **kwargs,
            )
        except stripe.error.InvalidRequestError:
            # Same race as above: a Stripe-side already-canceled sub cannot be
            # modified. The local flag still records the member's intent; the
            # ``deleted`` webhook / nightly reconcile terminalizes the row.
            logger.info(
                "subscription_stripe_cancel_already_terminal",
                subscription_id=str(subscription.pk),
                stripe_subscription_id=subscription.stripe_subscription_id,
            )
        except stripe.error.StripeError as exc:
            # Same retryable-502 treatment as the immediate branch: without it
            # the local flag would record an intent Stripe never accepted.
            raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
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

    # A pending downgrade makes the subscription schedule-managed on Stripe,
    # which rejects ``pause_collection``. Release the schedule first (clears
    # stripe_schedule_id + pending_plan locally) so the pause can proceed.
    from events.service.subscription_stripe_plan_change import release_online_schedule  # lazy: avoid cycle

    release_online_schedule(subscription)

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
