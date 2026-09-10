"""Service layer for membership subscriptions — all three payment methods.

Function-based service per the project's hybrid conventions. This module is the
orchestrator: it owns the local (OFFLINE / FREE) lifecycle outright and
*dispatches* to the Stripe modules for ONLINE rows — ``stripe.checkout``,
``stripe.plan_change``, ``stripe.sync``. It therefore
imports Stripe code rather than containing it, so every entry point here works
regardless of payment method and controllers never branch on one.

Lifecycle primitives shared with the Stripe modules live in
``subscription.core`` (re-exported here, so existing call sites are unaffected)
— that split is what keeps the service import graph acyclic. Plan CRUD and
price migration live in ``subscription.plans``.
"""

import functools
from datetime import timedelta

import stripe
import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SubscriptionActivationPendingError
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service import stripe_incidents

# ``create_subscription`` / ``record_payment`` / ``InitialPayment`` live in
# :mod:`events.service.subscription.core` (so the Stripe service can use them
# without importing this orchestrator back); re-exported here so existing call
# sites (controllers, tasks, tests) keep importing them from this module.
from events.service.subscription.core import (
    InitialPayment as InitialPayment,
)
from events.service.subscription.core import (
    create_subscription as create_subscription,
)
from events.service.subscription.core import (
    record_payment as record_payment,
)
from events.service.subscription.eligibility import ensure_tier_change_allowed
from events.service.subscription.notifications import (
    _dispatch_cancellation_confirmed,
    _dispatch_revival_checkout,
)
from events.service.subscription.sales import (
    ensure_member_not_excluded,
    ensure_plan_on_sale,
    ensure_plan_sales_capacity,
)
from events.service.subscription.stripe import checkout as stripe_checkout
from events.service.subscription.stripe import plan_change as stripe_plan_change
from events.service.subscription.stripe.payloads import stripe_account_kwargs

logger = structlog.get_logger(__name__)


# ---- Subscription operations -------------------------------------------------


def _expire_open_checkout_before_terminalizing(subscription: MembershipSubscription) -> None:
    """Kill the payable Checkout Session an immediate cancel is about to strand.

    An ONLINE row that carries a ``stripe_checkout_session_id`` but no
    ``stripe_subscription_id`` has a live hosted Checkout the member can still
    complete from an open tab. Terminalizing locally without expiring it means
    Stripe mints a Subscription that keeps billing while the local row is
    frozen: the completed-session handler refuses to link it (see
    :meth:`SubscriptionWebhookHandlersMixin.handle_subscription_checkout_completed`)
    and the reconcile sweep walks local rows, so nothing ever closes it.

    Same discipline as ``stripe_checkout._maybe_resume_pending_checkout``:
    expire first, and abort the cancel when the expire fails un-confirmably. A
    rejected expire is re-read rather than guessed at — the member completing
    the session mid-round-trip surfaces as the 409
    ``subscription_activation_pending`` (money has moved; cancel once the
    activation webhooks land), anything else as the module's retryable 502.

    The Stripe round trip happens under this member's row lock — the documented
    single-member blast radius the subscribe/revive paths already accept — and
    must, since the row must not be terminalized before the session is dead.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return
    if subscription.stripe_subscription_id or not subscription.stripe_checkout_session_id:
        return

    session_id = subscription.stripe_checkout_session_id
    kwargs = stripe_account_kwargs(subscription.organization)
    try:
        stripe.checkout.Session.expire(session_id, **kwargs)
    except stripe.error.InvalidRequestError:
        # Almost certainly "not in status open": already expired (nothing to
        # strand) or completed during our round trip (money moved). Re-read.
        try:
            session = stripe.checkout.Session.retrieve(session_id, **kwargs)
        except stripe.error.StripeError:
            logger.exception(
                "subscription_cancel_session_retrieve_failed",
                subscription_id=str(subscription.pk),
                stripe_checkout_session_id=session_id,
            )
            raise HttpError(502, str(_("Payment processing failed. Please try again later.")))
        if session.get("status") == "complete":
            logger.warning(
                "subscription_cancel_session_already_complete",
                subscription_id=str(subscription.pk),
                stripe_checkout_session_id=session_id,
            )
            raise SubscriptionActivationPendingError
        return
    except stripe.error.StripeError:
        logger.exception(
            "subscription_cancel_session_expire_failed",
            subscription_id=str(subscription.pk),
            stripe_checkout_session_id=session_id,
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later.")))
    logger.info(
        "subscription_cancel_session_expired",
        subscription_id=str(subscription.pk),
        stripe_checkout_session_id=session_id,
    )


def _expire_open_checkout_best_effort(subscription: MembershipSubscription) -> None:
    """Kill a still-payable Checkout Session a membership loss left behind.

    The non-raising twin of :func:`_expire_open_checkout_before_terminalizing`,
    for the ban / removal / GDPR-deletion path that must never fail because
    Stripe hiccuped. Runs post-commit (see the caller) so the round trip never
    happens under the row lock, which means the row is *already* terminal by the
    time we get here: every failure mode ends in "log it and move on".

    Losing the race stays survivable because the webhook backstops it — a session
    completed against a terminal row is refused its link and the Stripe
    Subscription it minted is cancelled by
    :meth:`SubscriptionWebhookHandlersMixin.handle_subscription_checkout_completed`
    — so we deliberately do not cancel anything from here. What that backstop
    cannot do is notice a session the banned member paid *before* it fires, so a
    rejected expire is re-read and a completed one raises the money alarm: the
    charge landed on a row nobody will ever link, and the refund is manual.

    The caller guarantees eligibility: ONLINE plan, a
    ``stripe_checkout_session_id``, and no ``stripe_subscription_id``.
    """
    session_id = subscription.stripe_checkout_session_id
    kwargs = stripe_account_kwargs(subscription.organization)
    try:
        stripe.checkout.Session.expire(session_id, **kwargs)
    except stripe.error.InvalidRequestError:
        # Almost certainly "not in status open": already expired (nothing to
        # strand) or completed before the ban landed (money moved). Re-read.
        try:
            session = stripe.checkout.Session.retrieve(session_id, **kwargs)
        except stripe.error.StripeError:
            logger.exception(
                "membership_loss_session_retrieve_failed",
                subscription_id=str(subscription.pk),
                stripe_checkout_session_id=session_id,
            )
            return
        if session.get("status") == "complete":
            stripe_incidents.record_subscription_checkout_while_terminal(
                subscription_id=str(subscription.pk),
                status=subscription.status,
                session_id=session_id,
                stripe_subscription_id=str(session.get("subscription") or ""),
            )
        return
    except stripe.error.StripeError:
        # The session may well still be payable; the terminal-row guard in the
        # completed-session webhook is what catches the member paying it anyway.
        logger.exception(
            "membership_loss_session_expire_failed",
            subscription_id=str(subscription.pk),
            stripe_checkout_session_id=session_id,
        )
        return
    logger.info(
        "membership_loss_session_expired",
        subscription_id=str(subscription.pk),
        stripe_checkout_session_id=session_id,
    )


@transaction.atomic
def cancel_subscription(
    subscription: MembershipSubscription,
    *,
    immediate: bool = False,
) -> MembershipSubscription:
    """Cancel a subscription.

    ``immediate=False`` (default) sets ``cancel_at_period_end`` and lets the
    grace-expiry task finish at the period boundary; ``immediate=True`` jumps
    straight to CANCELLED. A scheduled cancel with no boundary to wait for is
    refused (PAUSED) or upgraded to immediate — see the guards below.

    For Stripe-managed (ONLINE) subscriptions, dispatches to the Stripe
    service so the cancel is mirrored to Stripe; the webhook then settles
    local state. Falls back to the OFFLINE path when no Stripe link exists.
    """
    # Reload up front so the dispatch check sees committed plan/Stripe data.
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization")
        .get(pk=subscription.pk)
    )
    if subscription.is_terminal:
        return subscription

    prior_status = subscription.status
    prior_cap = subscription.cancel_at_period_end

    # A scheduled cancel needs a period boundary to land on, and the grace-expiry sweep only selects
    # ACTIVE-with-period / PAST_DUE. PAUSED freezes time (via pause_collection on Stripe for ONLINE), so it is
    # refused for both payment methods; a period-less row (PENDING OFFLINE, or an ONLINE row still sitting on an
    # unpaid Checkout Session) has nothing to wait for and is cancelled now — the immediate branch below expires
    # that still-payable session first, so the live URL cannot outlive the row. The one period-less row that IS
    # schedulable is one whose Stripe Subscription is already linked (the member paid; ``invoice.paid`` has not
    # been mirrored yet): Stripe owns that boundary, so ``cancel_at_period_end`` there honours the period they
    # just paid for instead of terminalizing it away.
    if not immediate and subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(
            400,
            str(_("Cannot schedule cancellation for a paused subscription. Resume it first, or cancel immediately.")),
        )
    if not immediate and subscription.current_period_end is None and not subscription.stripe_subscription_id:
        immediate = True

    if (
        subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
        and subscription.stripe_subscription_id
    ):
        subscription = stripe_checkout.cancel_online_subscription(subscription, immediate=immediate)
        # cancel_online_subscription mirrors local state synchronously, so the
        # dispatch gates below apply uniformly to both branches.
    elif immediate:
        # No Stripe subscription to cancel, but there may still be a live
        # Checkout Session minting one — kill it before the row goes terminal.
        _expire_open_checkout_before_terminalizing(subscription)
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
    else:
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])

    # Dispatch CANCELLATION_CONFIRMED based on what actually transitioned.
    # Gate 1: immediate cancel from a non-terminal status → fire immediate=True.
    # Gate 2: cancel_at_period_end flipped False→True for the first time → fire immediate=False.
    # Idempotent re-calls (flag already True, or subscription was already terminal) → no dispatch.
    if immediate and prior_status not in MembershipSubscription.TERMINAL_STATUSES:
        _dispatch_cancellation_confirmed(subscription, immediate=True)
    elif not immediate and not prior_cap and subscription.cancel_at_period_end:
        _dispatch_cancellation_confirmed(subscription, immediate=False)

    return subscription


@transaction.atomic
def cancel_subscriptions_for_membership_loss(user: RevelUser, organization: Organization) -> int:
    """Terminalize a user's subscriptions when they lose membership (ban / removal).

    Banning or removing a member must also stop their billing. Without this the
    next ``invoice.paid`` keeps charging a banned member (who now gets nothing)
    and, worse, re-creates a *removed* member as ACTIVE via
    :func:`subscription.stripe.sync._ensure_active_member` — silently undoing the
    staff action while billing continues.

    Mirrors :func:`subscription.refunds._cancel_refunded_subscription`: reload
    each non-terminal row under a ``select_for_update(of=("self",))`` lock,
    terminalize it locally (CANCELLED + ``cancelled_at``, clear
    ``cancel_at_period_end``), and for ONLINE rows schedule the matching Stripe
    close-out *after commit* — never a network call under the row lock: a
    best-effort cancel when a Stripe subscription is linked, a best-effort
    Checkout Session expire when the row only ever got as far as a session (its
    URL would otherwise stay payable after the ban). The member is told their
    subscription was cancelled (same dispatch as any immediate cancel).

    Never raises on Stripe failures: ban / removal / GDPR-deletion must go
    through regardless, so the Stripe side is deliberately best-effort and the
    webhook's terminal-row guard is the backstop for whatever slips past.

    Normally at most one non-terminal subscription exists per (user,
    organization), but this loops defensively.

    Returns the number of subscriptions cancelled.
    """
    # ponytail: refunds stay manual. Bans are for cause, so no money is returned
    # here (the platform fee is never refunded by design); staff retain the
    # org-admin refund endpoint for the rare good-faith removal.
    subscription_ids = list(
        MembershipSubscription.objects.filter(user=user, organization=organization)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .values_list("pk", flat=True)
    )
    cancelled = 0
    for sub_id in subscription_ids:
        subscription = (
            MembershipSubscription.objects.select_for_update(of=("self",))
            .select_related("plan", "plan__tier", "organization")
            .get(pk=sub_id)
        )
        if subscription.is_terminal:
            continue
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
        if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            if subscription.stripe_subscription_id:
                transaction.on_commit(
                    functools.partial(
                        stripe_checkout.cancel_stripe_subscription_best_effort,
                        subscription,
                        reason="membership_loss",
                    )
                )
            elif subscription.stripe_checkout_session_id:
                # Checkout never completed, so there is no Stripe Subscription to
                # cancel — but the hosted session's URL is still payable and would
                # outlive the ban. Kill it.
                transaction.on_commit(functools.partial(_expire_open_checkout_best_effort, subscription))
        _dispatch_cancellation_confirmed(subscription, immediate=True)
        cancelled += 1
    return cancelled


@transaction.atomic
def pause_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Pause a non-terminal subscription.

    For ONLINE (Stripe-managed) subscriptions, dispatches to the Stripe
    service so collection is paused on Stripe via ``pause_collection``.
    Refuses to pause an ONLINE subscription that has no linked Stripe
    record yet — pausing locally without telling Stripe would let invoices
    keep generating on the Stripe side while we believe collection is
    halted.
    """
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",)).select_related("plan").get(pk=subscription.pk)
    )
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot pause a terminal subscription.")))
    # Guard the reverse of ``cancel_subscription``'s PAUSED refusal: pausing a
    # subscription with a scheduled cancel would freeze it PAUSED+cancel_at_period_end,
    # invisible to the grace-expiry sweep (ACTIVE/PAST_DUE only). Placed before the
    # ONLINE dispatch so it covers both payment methods and never reaches Stripe.
    if subscription.cancel_at_period_end:
        raise HttpError(400, str(_("Cannot pause a subscription that is scheduled for cancellation.")))
    if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        if not subscription.stripe_subscription_id:
            raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
        return stripe_checkout.pause_online_subscription(subscription)
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        return subscription
    subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


@transaction.atomic
def resume_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Resume a PAUSED subscription back to ACTIVE.

    For ONLINE subscriptions, dispatches to the Stripe service so the
    matching ``pause_collection`` is cleared on Stripe. If the period has
    already lapsed, the next ``record_payment`` / grace-expiry pass will
    correct the status to PAST_DUE/EXPIRED. Mirrors the safety check in
    :func:`pause_subscription`: an ONLINE row without a Stripe link is
    refused outright rather than resumed locally.
    """
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization")
        .get(pk=subscription.pk)
    )
    if subscription.status != MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Only paused subscriptions can be resumed.")))
    if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        if not subscription.stripe_subscription_id:
            raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
        return stripe_checkout.resume_online_subscription(subscription)
    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def _validate_revivable(subscription: MembershipSubscription, *, member_facing: bool) -> None:
    """Run all revival pre-flight checks. Caller is responsible for locking.

    ``member_facing`` (the subscriber is the caller) puts refusals in the first
    person; see ``ensure_member_not_excluded`` for the non-disclosure rule.
    """
    if subscription.status != MembershipSubscription.SubscriptionStatus.EXPIRED:
        raise HttpError(400, str(_("Only expired subscriptions can be revived.")))

    # Same refusal as ``create_subscription``/``_validate_change_plan_target``/
    # ``start_online_subscription``. Beyond the OFFLINE case (landing a member on
    # a plan staff retired), ``archive_plan`` deactivates the Stripe Price and
    # ``create_revival_checkout`` only re-provisions when the id is *empty*, not
    # when it is inactive — so an ONLINE revival would 502 on Session.create,
    # after having already best-effort-cancelled the old Stripe subscription.
    if not subscription.plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))

    org = subscription.organization
    if org.membership_subscription_revival_window_days == 0:
        raise HttpError(400, str(_("Revival is disabled for this organization.")))

    if subscription.expired_at is None:
        raise HttpError(400, str(_("Cannot revive a subscription without an expiry timestamp.")))

    window_end = subscription.expired_at + timedelta(days=org.membership_subscription_revival_window_days)
    if timezone.now() > window_end:
        raise HttpError(400, str(_("The revival window has elapsed. Start a new subscription instead.")))

    has_other_active = (
        MembershipSubscription.objects.filter(user=subscription.user, organization=org)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exclude(pk=subscription.pk)
        .exists()
    )
    if has_other_active:
        if member_facing:
            raise HttpError(400, str(_("You already have an active subscription in this organization.")))
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    ensure_member_not_excluded(subscription.user, org, member_facing=member_facing)


def revive_subscription(
    subscription: MembershipSubscription,
    *,
    initial_payment: InitialPayment | None = None,
    revived_by: RevelUser | None = None,
    enforce_sales_status: bool = True,
) -> tuple[MembershipSubscription, str | None]:
    """Revive an EXPIRED subscription within the org's revival window.

    OFFLINE flow: caller must pass ``initial_payment``. The payment is
    recorded and the subscription transitions EXPIRED → ACTIVE under a
    short ``select_for_update`` transaction.

    ONLINE flow: mints a hosted Checkout Session for a fresh Stripe
    Subscription on the plan's current price and returns its URL. When the
    revival is staff-initiated (``revived_by`` is not the subscriber), the
    member is additionally emailed the checkout link — staff cannot pay on
    the member's behalf. Validation happens under a ``select_for_update``
    lock in an inner ``transaction.atomic()``. NOTE: under production
    ATOMIC_REQUESTS the inner block exit releases only a savepoint, so the
    row lock is in fact held across the Stripe calls until the request
    commits — accepted for now (single-member blast radius; the lock also
    serializes echo-webhooks against this mutation). That same lock is what
    lets the session-create call use a per-attempt Stripe ``idempotency_key``:
    concurrent attempts cannot interleave, so the key does not need to be
    derived from row state (and must not be — see ``create_revival_checkout``).

    Returns:
        A ``(subscription, checkout_url)`` tuple. ``checkout_url`` is
        ``None`` for OFFLINE revivals; the hosted Checkout Session URL for
        ONLINE revivals.

    Refuses if:
      - subscription is not EXPIRED
      - expired_at is None (legacy data)
      - revival window has elapsed
      - org disabled revival (revival_window_days == 0)
      - user has another non-terminal subscription
      - user is BANNED
      - OFFLINE revival called without initial_payment
      - plan sales are PAUSED (member callers; staff pass
        ``enforce_sales_status=False``) or the plan's cap is reached
        (everyone — the revived sub re-occupies a slot)
    """
    with transaction.atomic():
        subscription = (
            MembershipSubscription.objects.select_for_update(of=("self",))
            .select_related("plan", "plan__tier", "organization", "user")
            .get(pk=subscription.pk)
        )
        # Same "is this the subscriber themself?" test the checkout-email
        # dispatch below uses — here it decides the voice of every refusal.
        is_subscriber = revived_by is not None and revived_by.pk == subscription.user_id
        _validate_revivable(subscription, member_facing=is_subscriber)
        if enforce_sales_status:
            ensure_plan_on_sale(subscription.plan)
        # The EXPIRED row is terminal so it doesn't count against the cap —
        # reviving genuinely consumes a free slot or fails cleanly here.
        ensure_plan_sales_capacity(subscription.plan)
        is_online = subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE

        if not is_online:
            if initial_payment is None:
                raise HttpError(400, str(_("Offline revival requires recording an initial payment.")))

            # Materialize the membership the way ``create_subscription`` does for
            # OFFLINE plans. ``sync_member_from_subscription`` deliberately never
            # *creates* an OrganizationMember, and the row can genuinely be gone by
            # now: ``remove_member`` deletes it while leaving this (terminal, hence
            # skipped by ``cancel_subscriptions_for_membership_loss``) EXPIRED
            # subscription behind. Without this the revival records the payment and
            # reports an ACTIVE subscriber with no membership, tier or access.
            # ``_validate_revivable`` has already refused BANNED / hard-blacklisted
            # users, and the helper leaves a staff-PAUSED member paused.
            from events.service.subscription.stripe.sync import (  # lazy: avoid import cycle
                _ensure_active_member,
            )

            _ensure_active_member(subscription)

            subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
            # Revival consumed the expiry — clear it so a future lapse opens a
            # fresh revival window (history keeps the old value for audit).
            subscription.expired_at = None
            subscription.save(update_fields=["status", "expired_at", "updated_at"])
            record_payment(
                subscription,
                amount=initial_payment.amount,
                currency=initial_payment.currency,
                recorded_by=initial_payment.recorded_by,
                notes=initial_payment.notes,
                dispatch_renewal_notification=False,
            )

            logger.info(
                "membership_subscription_revived",
                subscription_id=str(subscription.pk),
                plan_id=str(subscription.plan_id),
                organization_id=str(subscription.organization_id),
                revived_by=str(revived_by.id) if revived_by else None,
                method="offline",
            )
            subscription.refresh_from_db()
            return subscription, None

    # ONLINE branch — the inner atomic block has exited, but under production
    # ATOMIC_REQUESTS the row lock is STILL held across the Stripe call until
    # the request commits (see docstring; accepted, single-member blast radius).
    checkout_url = stripe_checkout.create_revival_checkout(subscription)
    # Stripe call mutated and saved the subscription — refresh local state.
    subscription.refresh_from_db()
    if revived_by is not None and revived_by.pk != subscription.user_id:
        # Staff-initiated: the member has to complete the checkout themselves.
        _dispatch_revival_checkout(subscription, checkout_url=checkout_url)
    logger.info(
        "membership_subscription_revived",
        subscription_id=str(subscription.pk),
        plan_id=str(subscription.plan_id),
        organization_id=str(subscription.organization_id),
        revived_by=str(revived_by.id) if revived_by else None,
        method="online",
    )
    return subscription, checkout_url


def _validate_change_plan_target(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
    *,
    enforce_sales_status: bool,
) -> None:
    """Validate ``new_plan`` as a switch target for ``subscription``.

    Not the whole story for the member-facing endpoint: the OFFLINE
    payment-method refusal lives in ``me_subscriptions.change_plan`` (trust
    boundary — staff manage OFFLINE plans), so don't conclude that guard is
    missing when tracing from here.
    """
    if new_plan.tier.organization_id != subscription.organization_id:
        raise HttpError(400, str(_("New plan must belong to the same organization as the subscription.")))
    if not new_plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))
    if subscription.plan_id != new_plan.pk:
        if enforce_sales_status:
            ensure_plan_on_sale(new_plan)
        ensure_plan_sales_capacity(new_plan)
    if new_plan.payment_method != subscription.plan.payment_method:
        raise HttpError(
            400,
            str(_("Cannot switch between plans with different payment methods. Cancel and subscribe again instead.")),
        )
    if new_plan.currency.upper() != subscription.plan.currency.upper():
        raise HttpError(400, str(_("New plan must use the same currency as the current plan.")))


@transaction.atomic
def change_plan(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
    *,
    enforce_sales_status: bool = True,
) -> MembershipSubscription:
    """Switch ``subscription`` to ``new_plan``.

    For ONLINE subscriptions, dispatches to the Stripe service which routes
    to either an immediate prorated upgrade or a scheduled downgrade. Every
    other payment method takes the local branch below — an immediate, fee-free
    swap. For OFFLINE that means staff handle any settlement off-book; for FREE
    there is nothing to settle at all (both plans are zero-price, so no money
    moves in either direction).

    Refuses cross-organization plan changes and currency switches in either
    path; the latter would require manual prorating against a moving FX rate
    which we do not attempt. The target plan's subscription cap is always
    enforced; ``enforce_sales_status=False`` (staff callers) additionally
    skips the PAUSED-sales check.

    A **cross-tier** target additionally runs the membership eligibility gate
    stack against the destination tier (``ensure_tier_change_allowed``) —
    otherwise a member could hop from an ungated tier onto a gated one without
    ever passing its questionnaire or approval. Same-tier swaps are not
    re-gated.
    """
    ensure_tier_change_allowed(subscription, new_plan)
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization", "user")
        .get(pk=subscription.pk)
    )
    _validate_change_plan_target(subscription, new_plan, enforce_sales_status=enforce_sales_status)

    if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return stripe_plan_change.change_online_plan(subscription, new_plan)

    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot change the plan on a terminated subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Resume the subscription before changing its plan.")))
    if subscription.plan_id == new_plan.pk:
        raise HttpError(400, str(_("This subscription is already on that plan.")))

    subscription.plan = new_plan
    subscription.pending_plan = None
    subscription.save(update_fields=["plan", "pending_plan", "updated_at"])
    return subscription


# Plan CRUD and price migration live in :mod:`events.service.subscription.plans`;
# refund handling (``refund_payment`` + full-refund auto-cancel) lives in
# :mod:`events.service.subscription.refunds` (file-length cap).
