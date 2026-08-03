"""Stripe integration for membership subscriptions (Phase 2).

Lives next to ``subscription_service`` (the OFFLINE/staff-managed flow) and
adds the ONLINE flow on top of Stripe Connect direct charges. The local
state machine is still authoritative; Stripe events flow back via the
webhook handlers in :mod:`events.service.stripe_webhooks`.
"""

import time
import typing as t
import uuid
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
from common.models import SiteSettings
from common.service.vat_utils import b2b_vat_context
from common.utils import get_or_create_with_race_protection
from events.exceptions import SubscriptionActivationPendingError
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service import subscription_core, subscription_sales
from events.service.subscription_stripe_base import (
    _require_stripe_connected,
)
from events.service.subscription_stripe_base import (
    ensure_stripe_price as ensure_stripe_price,
)
from events.service.subscription_stripe_payloads import (
    _is_subscription_gone,
    _stripe_account_kwargs,
)
from events.service.subscription_stripe_plan_change import (
    release_online_schedule,
    resolve_refused_cancel,
)

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service):
# this module makes its own outbound calls and must not rely on another
# module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


# ---- Customer profile --------------------------------------------------------


def ensure_customer_profile(user: RevelUser, organization: Organization) -> CustomerProfile:
    """Return the per-(user, organization) Stripe Customer, creating it if needed."""
    _require_stripe_connected(organization)
    existing = CustomerProfile.objects.filter(user=user, organization=organization).first()
    if existing:
        return existing

    try:
        customer = stripe.Customer.create(
            # No `name`: the user's display name stays out of Stripe payloads (#848).
            email=user.email,
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
# ``ensure_stripe_price`` lives in :mod:`subscription_stripe_base` (shared with
# ``subscription_stripe_plan_change``) and is re-exported via the import above.


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
    redirect back with a query flag the frontend reads. Points at the dedicated
    membership route (``/org/<id>/membership``, revel-frontend#720) — the org
    landing page no longer renders the checkout outcome (#838). UUID-based so the
    org slug never reaches Stripe; the FE resolves UUIDs (#848 / revel-frontend#756).
    """
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    return {
        "success_url": f"{frontend_base_url}/org/{organization.id}/membership?membership_success=true",
        "cancel_url": f"{frontend_base_url}/org/{organization.id}/membership?membership_cancelled=true",
    }


def effective_application_fee_percent(org: Organization) -> Decimal | None:
    """Org fee percent grossed up with platform VAT when applicable.

    Tickets charge the org fee + VAT on the fee (``calculate_platform_fee_vat``
    adds VAT on top). ``application_fee_percent`` is the only fee mechanism for
    subscriptions (Stripe has no fixed/absolute variant), so the same economics
    are achieved by grossing the percent itself up. The fixed fee component
    (``org.platform_fee_fixed``) intentionally does NOT apply to subscriptions.

    Returns:
        The percent to send to Stripe, or ``None`` when no fee applies.
    """
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


class FeeResyncCounters(t.TypedDict):
    """Telemetry counters returned by :func:`resync_subscription_application_fees`."""

    updated: int
    skipped_schedule_managed: int
    failed: int


def resync_subscription_application_fees(org: Organization, *, sleep_seconds: float = 0.0) -> FeeResyncCounters:
    """Push the org's *current* effective fee percent onto its live Stripe subscriptions.

    The grossed-up ``application_fee_percent`` is frozen into each Stripe
    Subscription at Checkout; when the org's VAT status later changes (VIES
    revalidation, VAT ID set/cleared, country change) the frozen percent stops
    matching the fee the ledger decomposition assumes. This resyncs every
    non-terminal ONLINE subscription to the value Checkout would send today.
    A ``None`` percent (fee-free org) clears the fee on Stripe (``""`` unsets).

    Schedule-managed subscriptions (pending downgrades) are **skipped**: Stripe
    rejects a plain ``Subscription.modify`` while a schedule is attached, and
    releasing the schedule would silently drop the pending plan change. They
    are counted so callers can surface them; re-run the
    ``resync_subscription_fees`` management command once the schedule releases.

    Per-subscription Stripe failures are logged and counted, not raised — one
    bad subscription must not strand the rest of the org's resync.

    Args:
        org: The organization whose subscriptions to resync.
        sleep_seconds: Optional pause between Stripe calls (rate limiting for
            large backfills; the org's subscriptions all live on one Connect
            account).

    Returns:
        Counters for updated / skipped (schedule-managed) / failed rows.
    """
    target = effective_application_fee_percent(org)
    kwargs = _stripe_account_kwargs(org)
    subscriptions = (
        MembershipSubscription.objects.filter(
            organization=org,
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        .exclude(stripe_subscription_id="")
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
    )
    counters: FeeResyncCounters = {"updated": 0, "skipped_schedule_managed": 0, "failed": 0}
    for subscription in subscriptions:
        if subscription.stripe_schedule_id:
            counters["skipped_schedule_managed"] += 1
            logger.warning(
                "subscription_fee_resync_skipped_schedule_managed",
                subscription_id=str(subscription.pk),
                org_id=str(org.pk),
                schedule_id=subscription.stripe_schedule_id,
            )
            continue
        try:
            stripe.Subscription.modify(
                t.cast(str, subscription.stripe_subscription_id),
                application_fee_percent=float(target) if target is not None else "",
                **kwargs,
            )
        except stripe.error.StripeError as exc:
            counters["failed"] += 1
            logger.error(
                "subscription_fee_resync_failed",
                subscription_id=str(subscription.pk),
                org_id=str(org.pk),
                error=str(exc),
            )
            continue
        counters["updated"] += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
    logger.info(
        "subscription_fee_resync_done",
        org_id=str(org.pk),
        target_percent=str(target) if target is not None else None,
        **counters,
    )
    return counters


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
    effective_percent = effective_application_fee_percent(org)
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

    Creates the local row via :func:`subscription_core.create_subscription`
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
    subscription = subscription_core.create_subscription(plan, user)

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

    Raises :class:`SubscriptionActivationPendingError` (409, carrying the
    ``subscription_activation_pending`` code) when the session already
    completed or the Stripe Subscription is already linked (payment confirmed
    but the activation webhooks haven't landed yet) — creating a second
    subscription there would double-charge, and the frontend needs to tell
    that apart from a plain duplicate-subscription refusal.
    """
    # Locked read: two concurrent subscribes (double-submit, stale-tab retry
    # with a different plan) must not diverge — one expiring the session while
    # the other hands its URL back (a dead Checkout page). The lock is
    # per-member (single-member blast radius) and, under ATOMIC_REQUESTS, is
    # held across the Session.retrieve below — same accepted trade as the
    # revive path (see #702's reservation-lock precedent for tickets).
    pending = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .filter(
            organization=plan.tier.organization,
            user=user,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            # ONLINE only: a staff-created OFFLINE PENDING row owns no Checkout
            # Session and would simply be deleted here; leaving it alone lets
            # ``create_subscription``'s duplicate-active check refuse the subscribe.
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        .select_related("plan", "organization")
        .first()
    )
    if pending is None:
        return None

    if pending.stripe_subscription_id:
        # A PENDING row only gets its Stripe Subscription id when its session
        # completed — payment is done, activation webhooks are in flight.
        raise SubscriptionActivationPendingError

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
        raise SubscriptionActivationPendingError

    if session_status == "open" and pending.plan_id == plan.pk and session.url:
        logger.info(
            "subscription_stripe_pending_checkout_resumed",
            subscription_id=str(pending.pk),
            stripe_checkout_session_id=pending.stripe_checkout_session_id,
        )
        return pending, t.cast(str, session.url)

    # Different plan, or the session expired: expire the old session, then clear
    # the local row.
    #
    # The expire must succeed before the row goes. The member is holding a live,
    # payable URL for the old session; if we drop the row while that session is
    # still open and they then pay it, ``checkout.session.completed`` matches
    # nothing, the dedup row still commits HANDLED so Stripe never retries, and
    # the reconcile sweep — which walks local rows — can never discover the
    # resulting subscription. That is captured money with no row, no membership
    # and no ledger entry, so a failed expire has to abort instead.
    if session_status == "open":
        try:
            stripe.checkout.Session.expire(
                pending.stripe_checkout_session_id,
                **_stripe_account_kwargs(pending.organization),
            )
        except stripe.error.InvalidRequestError:
            # Almost certainly "not in status open" — the member completed the
            # session during our round trip. Re-read rather than guess.
            logger.warning(
                "subscription_stripe_pending_session_expire_rejected",
                subscription_id=str(pending.pk),
                stripe_checkout_session_id=pending.stripe_checkout_session_id,
            )
            raise SubscriptionActivationPendingError
        except stripe.error.StripeError:
            logger.exception(
                "subscription_stripe_pending_session_expire_failed",
                subscription_id=str(pending.pk),
                stripe_checkout_session_id=pending.stripe_checkout_session_id,
            )
            raise HttpError(502, str(_("Payment processing failed. Please try again later.")))
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


StalePendingVerdict = t.Literal["clear", "paid", "skip"]


def classify_stale_pending_checkout(pending: MembershipSubscription) -> StalePendingVerdict:
    """Ask Stripe whether an untouched PENDING row's Checkout Session is really dead.

    Age alone does not make a PENDING row abandoned. If
    ``checkout.session.completed`` kept failing — every redelivery rolls the
    link back — the session can be ``complete`` with money captured while the
    row still looks like a dropped redirect. Clearing it there destroys the only
    handle back to the payment: later webhooks match no row, and the reconcile
    sweep walks local rows, so the member is left with a charge, no membership
    and no ledger entry. Same hazard :func:`_maybe_resume_pending_checkout`
    guards on the member-facing path, so it is observed the same way.

    Makes a Stripe round trip: call it OUTSIDE the row lock, then re-check the
    row inside the lock before acting on the verdict.

    Returns:
        ``"clear"`` — nothing was ever payable (no session id) or Stripe reports
        the session ``expired``: the row only holds a cap slot.
        ``"paid"`` — the session is ``complete``: keep the row and raise an
        incident; the webhook or a later reconcile can still link it.
        ``"skip"`` — the retrieve failed, or the session is still ``open`` (it
        carries an ``expires_at``, so it dies on its own). Leave the row for the
        next nightly run.
    """
    if not pending.stripe_checkout_session_id:
        return "clear"
    try:
        session = stripe.checkout.Session.retrieve(
            pending.stripe_checkout_session_id,
            **_stripe_account_kwargs(pending.organization),
        )
    except stripe.error.StripeError:
        logger.exception(
            "subscription_stale_pending_session_retrieve_failed",
            subscription_id=str(pending.pk),
            stripe_checkout_session_id=pending.stripe_checkout_session_id,
        )
        return "skip"
    session_status = t.cast(str, session.status or "")
    if session_status == "complete":
        return "paid"
    if session_status == "expired":
        return "clear"
    logger.info(
        "subscription_stale_pending_session_not_settled",
        subscription_id=str(pending.pk),
        stripe_checkout_session_id=pending.stripe_checkout_session_id,
        session_status=session_status,
    )
    return "skip"


def cancel_stripe_subscription_best_effort(subscription: MembershipSubscription, *, reason: str) -> bool:
    """Best-effort ``stripe.Subscription.cancel`` for a locally-terminalized row.

    Local terminalization (grace expiry, revival superseding an old sub) must
    close the Stripe side too, or Smart Retries keep dunning a member who has
    already lost access locally (C1/C2 in the 2026-06-10 reassessment). Errors
    are logged, never raised: the local state machine stays authoritative.

    The nightly reconciliation retries a failed cancel — it re-issues this call
    for any terminal row Stripe still reports as live — but only while the row
    still *carries* ``stripe_subscription_id``. A caller about to discard that id
    (and thus the only handle back to the Stripe object) is outside that safety
    net and must check the return value rather than assume a later sweep will
    clean up.

    Returns True when the Stripe side is known to be closed — already gone, or
    cancelled after releasing a downgrade schedule Stripe was refusing over — so
    callers can treat ``False`` as "the Stripe subscription may still be live and
    billing" rather than having to re-derive that. ``False`` also covers the
    no-id no-op; check ``stripe_subscription_id`` first when that matters.
    """
    if not subscription.stripe_subscription_id:
        return False
    try:
        stripe.Subscription.cancel(  # type: ignore[attr-defined]
            subscription.stripe_subscription_id,
            **_stripe_account_kwargs(subscription.organization),
        )
    except stripe.error.InvalidRequestError as exc:
        # Already gone → the desired end state holds. Schedule-managed (pending
        # downgrade) → release the schedule and retry once. Anything else → False.
        return resolve_refused_cancel(subscription, exc, reason=reason)
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
    #
    # This one is NOT best-effort in practice: the id is cleared a few lines
    # down, so a silent failure here leaves a live Stripe subscription that no
    # local row references and that the nightly reconcile — which iterates local
    # rows — can never rediscover. Fail the revival instead and let the member
    # retry; the row is untouched, so a retry is safe.
    if subscription.stripe_subscription_id and not cancel_stripe_subscription_best_effort(
        subscription, reason="revival_supersedes"
    ):
        raise HttpError(502, str(_("Payment processing failed. Please try again later.")))

    if not plan.stripe_price_id:
        plan = ensure_stripe_price(plan)
        subscription.plan = plan

    customer = ensure_customer_profile(subscription.user, org)

    # Per-attempt idempotency key. It cannot be anchored on ``expired_at``: an
    # abandoned revival is reverted to EXPIRED with ``expired_at`` deliberately
    # preserved (see ``_clear_stale_pending_checkout``), so the key would repeat
    # while ``_create_subscription_checkout_session`` recomputes ``expires_at``
    # from ``now()`` — same key, different params, which Stripe rejects with an
    # idempotency error for the ~24h the key is cached, 502-ing every retry.
    # Concurrent attempts are already serialized by the caller's row lock, and a
    # ``mode=subscription`` session only charges on completion, so an orphaned
    # session from a network-timeout retry cannot double-charge.
    idempotency_key = f"sub-revival:{subscription.pk}:{uuid.uuid4().hex}"

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


def _tolerate_gone_or_raise(subscription: MembershipSubscription, exc: stripe.error.InvalidRequestError) -> None:
    """Swallow Stripe's refusal only when the subscription is already gone.

    Tolerated case: the sub was canceled/deleted Stripe-side (staff used the
    Dashboard and this call races the ``deleted`` webhook, or it runs inside
    the ``charge.refunded`` auto-cancel). The caller's intent is "make it
    canceled", so local terminalization proceeds; raising would 500 the
    request/webhook and wedge Stripe redelivery in a retry loop.

    Anything else — most importantly a *schedule-managed* subscription, which
    Stripe refuses to modify — means Stripe is still billing, so it surfaces as
    the module's retryable 502 and local state stays untouched.
    """
    if not _is_subscription_gone(exc):
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    logger.info(
        "subscription_stripe_cancel_already_terminal",
        subscription_id=str(subscription.pk),
        stripe_subscription_id=subscription.stripe_subscription_id,
    )


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
    release_online_schedule(subscription)

    if immediate:
        try:
            # ``Subscription.cancel`` is the documented runtime API; the type stubs
            # don't expose it as a classmethod, hence the ignore.
            stripe.Subscription.cancel(subscription.stripe_subscription_id, **kwargs)  # type: ignore[attr-defined]
        except stripe.error.InvalidRequestError as exc:
            _tolerate_gone_or_raise(subscription, exc)
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
        except stripe.error.InvalidRequestError as exc:
            # Same tolerance as the immediate branch: record the member's intent
            # only when Stripe has nothing left to cancel.
            _tolerate_gone_or_raise(subscription, exc)
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

    Callers must filter out schedule-managed rows (``stripe_schedule_id`` set)
    first — Stripe rejects a plain ``Subscription.modify`` while a schedule is
    attached, so this raises the module's 502 rather than migrating them. See
    ``subscription_service.migrate_plan_subscribers`` and
    :func:`resync_subscription_application_fees` for the carve-out.

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
