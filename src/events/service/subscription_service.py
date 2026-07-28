"""Service layer for membership subscriptions (Phase 1, OFFLINE).

Function-based service per the project's hybrid conventions. Stripe-specific
logic is intentionally absent; it lands in a separate Phase 2 module.
"""

import dataclasses
import datetime
import functools
import typing as t
from datetime import timedelta
from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription_sales import (
    ensure_member_not_excluded,
    ensure_plan_on_sale,
    ensure_plan_sales_capacity,
)
from events.service.ticket_service import check_online_payment_prerequisites
from events.utils.subscription_periods import calculate_period_end

logger = structlog.get_logger(__name__)


@dataclasses.dataclass
class InitialPayment:
    """Payload bundling the optional first payment recorded with a subscription."""

    amount: Decimal
    currency: str
    recorded_by: RevelUser
    notes: str = ""


def _validate_occurred_at(
    subscription: MembershipSubscription,
    occurred_at: datetime.datetime,
    now: datetime.datetime,
) -> None:
    """Reject occurred_at values that don't belong to the subscription's timeline."""
    if occurred_at > now:
        raise HttpError(400, str(_("occurred_at cannot be in the future.")))
    if occurred_at < subscription.created_at:
        raise HttpError(400, str(_("occurred_at cannot predate the subscription.")))
    if subscription.current_period_start and occurred_at < subscription.current_period_start:
        raise HttpError(
            400,
            str(_("occurred_at cannot predate the start of the current billing period.")),
        )
    if (
        subscription.current_period_end
        and subscription.current_period_end < now
        and occurred_at < subscription.current_period_end
    ):
        raise HttpError(
            400,
            str(_("occurred_at cannot predate the lapsed period end of the subscription.")),
        )


# ---- Plan operations ---------------------------------------------------------


def _maybe_sync_plan_to_stripe(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Provision (or refresh) the Stripe Product+Price for an ONLINE plan.

    No-op for OFFLINE plans. Stripe failures bubble up as ``HttpError`` so the
    controller can return a clean ``502``; the DB transaction rolls back along
    with the plan write so we don't leave a half-provisioned row.

    Raises:
        StripeNotConnectedError: If the organization has no Stripe Connect account.
        BillingInfoRequiredError: If platform fees apply but billing info is incomplete.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return plan
    # ONLINE plans generate platform fees, so the org must be invoiceable — same
    # gate ONLINE ticket tiers pass through. Runs before ``ensure_stripe_price``
    # (whose own connectivity guard raises a generic 400) so callers get the
    # typed exceptions and their actionable messages.
    check_online_payment_prerequisites(plan.tier.organization)

    from events.service import subscription_stripe_service  # lazy: avoid cycle

    return subscription_stripe_service.ensure_stripe_price(plan)


@transaction.atomic
def create_plan(
    tier: MembershipTier,
    *,
    name: str,
    price: Decimal,
    currency: str,
    period_unit: str,
    period_count: int = 1,
    description: str = "",
    is_active: bool = True,
    payment_method: str = MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    sales_status: str = MembershipSubscriptionPlan.SalesStatus.OPEN,
    max_subscriptions: int | None = None,
) -> MembershipSubscriptionPlan:
    """Create a subscription plan for a membership tier.

    For ONLINE plans, also provisions the matching Stripe Product+Price on
    the organization's Connect account.
    """
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=name,
        price=price,
        currency=currency,
        period_unit=period_unit,
        period_count=period_count,
        description=description,
        is_active=is_active,
        payment_method=payment_method,
        sales_status=sales_status,
        max_subscriptions=max_subscriptions,
    )
    return _maybe_sync_plan_to_stripe(plan)


@transaction.atomic
def update_plan(
    plan: MembershipSubscriptionPlan,
    **fields: t.Any,
) -> MembershipSubscriptionPlan:
    """Update a plan in-place.

    Callers pass only the fields to change; full_clean runs on save. When the
    plan is ONLINE and any pricing-shape field changes, the Stripe Price is
    archived and a fresh one created (Stripe Prices are immutable).

    Refuses currency changes when the plan has any non-terminal subscriptions
    — cross-currency migration is risky and out of roadmap; staff must archive
    and create a new plan instead.
    """
    if not fields:
        return plan

    new_currency = fields.get("currency")
    if new_currency is not None and new_currency.upper() != plan.currency.upper():
        has_active_subs = (
            MembershipSubscription.objects.filter(plan=plan)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .exists()
        )
        if has_active_subs:
            msg = _("Cannot change currency when active subscriptions exist. Archive and create a new plan instead.")
            raise HttpError(400, str(msg))

    for field, value in fields.items():
        setattr(plan, field, value)
    plan.save(update_fields=[*fields.keys(), "updated_at"])
    return _maybe_sync_plan_to_stripe(plan)


@transaction.atomic
def archive_plan(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Soft-disable a plan by flipping ``is_active``.

    For ONLINE plans, also archives the Stripe Price so it can't be used for
    new subscriptions. Existing subscribers keep paying their old Price.
    """
    if plan.is_active:
        plan.is_active = False
        plan.save(update_fields=["is_active", "updated_at"])
    if plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        from events.service import subscription_stripe_service

        subscription_stripe_service.archive_stripe_price(plan)
    return plan


@transaction.atomic
def delete_plan(plan: MembershipSubscriptionPlan) -> None:
    """Hard-delete a plan.

    Raises 400 if any subscription references it — staff should archive
    instead.
    """
    if plan.subscriptions.exists():
        raise HttpError(400, str(_("Cannot delete a plan with existing subscriptions. Archive it instead.")))
    try:
        plan.delete()
    except ProtectedError as exc:
        # Concurrent ``create_subscription`` slipped in between our existence
        # check and the delete: PROTECT raises ProtectedError which would
        # otherwise bubble up as a 500.
        raise HttpError(400, str(_("Cannot delete a plan with existing subscriptions. Archive it instead."))) from exc


# ---- Subscription operations -------------------------------------------------


@transaction.atomic
def create_subscription(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    initial_payment: InitialPayment | None = None,
) -> MembershipSubscription:
    """Create a subscription for ``user`` on ``plan``.

    Refuses if the user is BANNED in the organization, or already has a
    non-terminal subscription there, or the plan's subscription cap is
    reached. Ensures an :class:`OrganizationMember` exists at the plan's
    tier in the same transaction.
    """
    organization = plan.tier.organization

    if not plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))

    # Refuse BANNED.
    banned = OrganizationMember.objects.filter(
        organization=organization,
        user=user,
        status=OrganizationMember.MembershipStatus.BANNED,
    ).exists()
    if banned:
        raise HttpError(403, str(_("This user is banned from the organization.")))

    # Refuse hard-blacklisted (defense-in-depth: the member-facing controller
    # already 404s an invisible org, but staff-initiated and other call sites
    # reach the service directly). Same helper the membership BlacklistGate uses.
    from events.service.blacklist_service import check_user_hard_blacklisted  # lazy: avoid cycle

    if check_user_hard_blacklisted(user, organization):
        raise HttpError(403, str(_("This user is blacklisted from the organization.")))

    # Refuse duplicate active subscription.
    duplicate = (
        MembershipSubscription.objects.filter(organization=organization, user=user)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exists()
    )
    if duplicate:
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    ensure_plan_sales_capacity(plan)

    # Ensure membership exists at plan.tier (don't overwrite BANNED — guarded above).
    # ONLINE plans gate ACTIVE membership on the first successful Stripe payment,
    # so we don't grant tier benefits up front: that work moves into the
    # ``invoice.paid`` / ``customer.subscription.updated`` webhook handlers.
    if plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE:
        OrganizationMember.objects.update_or_create(
            organization=organization,
            user=user,
            defaults={
                "tier": plan.tier,
                "status": OrganizationMember.MembershipStatus.ACTIVE,
            },
        )

    # The partial-unique index protects against a race where two requests both
    # pass the duplicate check above; the helper re-fetches the winner whether
    # the race surfaces as IntegrityError (INSERT) or ValidationError
    # (TimeStampedModel.save's full_clean sees the committed racing row).
    subscription, created = get_or_create_with_race_protection(
        MembershipSubscription,
        Q(user=user, organization=organization) & ~Q(status__in=MembershipSubscription.TERMINAL_STATUSES),
        defaults={
            "user": user,
            "plan": plan,
            "organization": organization,
            "status": MembershipSubscription.SubscriptionStatus.PENDING,
        },
    )
    if not created:
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    if initial_payment is not None:
        record_payment(
            subscription,
            amount=initial_payment.amount,
            currency=initial_payment.currency,
            recorded_by=initial_payment.recorded_by,
            notes=initial_payment.notes,
        )
        # record_payment mutates a freshly-locked instance; refresh ours so
        # callers see the advanced period and ACTIVE status without a manual
        # refresh_from_db().
        subscription.refresh_from_db()

    return subscription


@transaction.atomic
def record_payment(
    subscription: MembershipSubscription,
    *,
    amount: Decimal,
    currency: str,
    recorded_by: RevelUser | None,
    notes: str = "",
    status: str = MembershipPayment.PaymentStatus.SUCCEEDED,
    occurred_at: datetime.datetime | None = None,
    dispatch_renewal_notification: bool = True,
) -> MembershipPayment:
    """Record a payment and advance the subscription's billing period.

    A SUCCEEDED payment advances the period and resets PENDING/PAST_DUE to
    ACTIVE. Terminal subscriptions (CANCELLED, EXPIRED) refuse the payment
    entirely — staff must create a fresh subscription instead.

    ``occurred_at`` lets staff backfill historical payments. When set, it
    becomes the anchor for ``period_start`` / ``period_end`` and is persisted
    on the row so callers can render ``occurred_at ?? created_at`` consistently.

    ``dispatch_renewal_notification`` controls whether a
    SUBSCRIPTION_RENEWAL_SUCCEEDED notification is fired.  Pass ``False`` when
    the caller will handle the notification itself (e.g. the H1 revival flow)
    or when the payment is the *first* payment of a new subscription (prior
    status was PENDING — handled automatically by the gate below).
    """
    subscription = MembershipSubscription.objects.select_for_update().get(pk=subscription.pk)
    prior_status = subscription.status
    if subscription.is_terminal:
        raise HttpError(
            400,
            str(
                _(
                    "Cannot record a payment against a cancelled or expired subscription. "
                    "Create a new subscription instead."
                )
            ),
        )
    plan = subscription.plan
    now = timezone.now()

    if occurred_at is not None:
        _validate_occurred_at(subscription, occurred_at, now)

    anchor = occurred_at or now

    advance = status == MembershipPayment.PaymentStatus.SUCCEEDED
    period_start = (
        subscription.current_period_end
        if (advance and subscription.current_period_end and subscription.current_period_end > anchor)
        else anchor
    )
    period_end = calculate_period_end(period_start, plan) if advance else (subscription.current_period_end or anchor)

    if advance and occurred_at is not None and period_end < now:
        # Refuse backfills that would leave the subscription ACTIVE with an already-lapsed
        # period (callers checking only ``status`` would grant access until the expiry beat).
        raise HttpError(
            400,
            str(_("Backfilled payment would produce an already-lapsed billing period; use a more recent occurred_at.")),
        )

    payment = MembershipPayment.objects.create(
        subscription=subscription,
        amount=amount,
        currency=currency,
        status=status,
        period_start=period_start,
        period_end=period_end,
        occurred_at=occurred_at,
        recorded_by=recorded_by,
        notes=notes,
    )

    if not advance:
        return payment

    update_fields = ["current_period_start", "current_period_end", "updated_at"]
    subscription.current_period_start = period_start
    subscription.current_period_end = period_end

    revivable = {
        MembershipSubscription.SubscriptionStatus.PENDING.value,
        MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    }
    if subscription.status in revivable:
        subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        update_fields.append("status")
        if subscription.expired_at:
            # A reactivated row consumed its expiry: a later lapse must stamp a
            # FRESH expired_at, or the revival window/deadline math anchors on
            # the stale first expiry.
            # The audit trail lives in simple-history.
            subscription.expired_at = None
            update_fields.append("expired_at")

    subscription.save(update_fields=update_fields)

    _renewal_eligible_statuses = {
        MembershipSubscription.SubscriptionStatus.ACTIVE.value,
        MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    }
    if dispatch_renewal_notification and prior_status in _renewal_eligible_statuses:
        _dispatch_renewal_succeeded(subscription)

    return payment


@transaction.atomic
def cancel_subscription(
    subscription: MembershipSubscription,
    *,
    immediate: bool = False,
) -> MembershipSubscription:
    """Cancel a subscription.

    ``immediate=False`` (default) sets ``cancel_at_period_end`` and lets the
    grace-expiry task finish the cancellation at the period boundary.
    ``immediate=True`` jumps straight to CANCELLED. PAUSED subscriptions
    refuse the scheduled path — pause freezes time so the period boundary
    would never be reached; callers must resume first or cancel immediately.

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

    # PAUSED refuses the scheduled path for BOTH payment methods: pause freezes
    # time (locally for OFFLINE, via pause_collection on Stripe for ONLINE), so
    # the period boundary the scheduled cancel waits for is never reached — the
    # row would sit PAUSED+cancel_at_period_end forever, invisible to the
    # grace-expiry sweep (which only selects ACTIVE/PAST_DUE).
    if not immediate and subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(
            400,
            str(_("Cannot schedule cancellation for a paused subscription. Resume it first, or cancel immediately.")),
        )

    if (
        subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
        and subscription.stripe_subscription_id
    ):
        # Lazy import to avoid a service<->stripe-service cycle.
        from events.service import subscription_stripe_service

        subscription = subscription_stripe_service.cancel_online_subscription(subscription, immediate=immediate)
        # cancel_online_subscription mirrors local state synchronously, so the
        # dispatch gates below apply uniformly to both branches.
    elif immediate:
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
    :func:`subscription_stripe_sync._ensure_active_member` — silently undoing the
    staff action while billing continues.

    Mirrors :func:`subscription_refunds._cancel_refunded_subscription`: reload
    each non-terminal row under a ``select_for_update(of=("self",))`` lock,
    terminalize it locally (CANCELLED + ``cancelled_at``, clear
    ``cancel_at_period_end``), and for ONLINE rows with a linked Stripe
    subscription schedule a best-effort Stripe cancel *after commit* — never a
    network call under the row lock. The member is told their subscription was
    cancelled (same dispatch as any immediate cancel).

    Normally at most one non-terminal subscription exists per (user,
    organization), but this loops defensively.

    Returns the number of subscriptions cancelled.
    """
    # ponytail: refunds stay manual. Bans are for cause, so no money is returned
    # here (the platform fee is never refunded by design); staff retain the
    # org-admin refund endpoint for the rare good-faith removal.
    from events.service import subscription_stripe_service  # lazy: avoid cycle

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
        if (
            subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
            and subscription.stripe_subscription_id
        ):
            transaction.on_commit(
                functools.partial(
                    subscription_stripe_service.cancel_stripe_subscription_best_effort,
                    subscription,
                    reason="membership_loss",
                )
            )
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
        from events.service import subscription_stripe_service  # lazy: avoid cycle

        return subscription_stripe_service.pause_online_subscription(subscription)
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
        from events.service import subscription_stripe_service  # lazy: avoid cycle

        return subscription_stripe_service.resume_online_subscription(subscription)
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
    from events.service import subscription_stripe_service  # lazy: avoid cycle

    checkout_url = subscription_stripe_service.create_revival_checkout(subscription)
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
    """Validate ``new_plan`` as a switch target for ``subscription``."""
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
            str(_("Cannot switch between ONLINE and OFFLINE plans. Cancel and create a new subscription instead.")),
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
    to either an immediate prorated upgrade or a scheduled downgrade. OFFLINE
    subscriptions perform an immediate, fee-free swap — staff are expected
    to handle any settlement off-book.

    Refuses cross-organization plan changes and currency switches in either
    path; the latter would require manual prorating against a moving FX rate
    which we do not attempt. The target plan's subscription cap is always
    enforced; ``enforce_sales_status=False`` (staff callers) additionally
    skips the PAUSED-sales check.
    """
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization", "user")
        .get(pk=subscription.pk)
    )
    _validate_change_plan_target(subscription, new_plan, enforce_sales_status=enforce_sales_status)

    if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        from events.service import subscription_stripe_plan_change  # lazy: avoid cycle

        return subscription_stripe_plan_change.change_online_plan(subscription, new_plan)

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


class MigrationError(t.TypedDict):
    """Per-subscription error record produced by :func:`migrate_plan_subscribers`."""

    sub_id: str
    reason: str


class MigrationResult(t.TypedDict):
    """Aggregate result of a :func:`migrate_plan_subscribers` call."""

    migrated: int
    skipped: int
    failed: int
    errors: list[MigrationError]


def migrate_plan_subscribers(
    plan: MembershipSubscriptionPlan,
    *,
    initiated_by: RevelUser,
) -> MigrationResult:
    """Force-migrate non-terminal subscriptions on ``plan`` to its current price.

    For ONLINE subs: calls subscription_stripe_service.update_subscription_price,
    which issues stripe.Subscription.modify(proration_behavior='none'). The new
    price takes effect at the next renewal.

    For OFFLINE subs: no Stripe call; just notifies that next renewal will be
    at the new amount.

    Per-subscription errors are captured in result["errors"]; successful
    migrations are not rolled back. Re-running the endpoint after a partial
    failure is safe: ONLINE subs already on the current Stripe price are
    counted as ``skipped``, and OFFLINE subs whose price-change notice for
    this exact change was already sent are ``skipped`` too — the migration
    writes nothing to an OFFLINE row, so the notification ledger is the
    idempotency anchor that keeps a re-run (staff double-click, acks_late
    redelivery) from re-spamming subscribers.
    """
    from events.service import subscription_stripe_service  # lazy: avoid cycle

    result: MigrationResult = {"migrated": 0, "skipped": 0, "failed": 0, "errors": []}
    qs = (
        MembershipSubscription.objects.filter(plan=plan)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .select_related("plan", "organization", "user")
    )
    new_price = plan.price

    # Single-query lookup for the subscriber's last SUCCEEDED payment amount,
    # to avoid N+1 inside the migration loop. Postgres DISTINCT ON picks the
    # most-recent row per subscription according to the ORDER BY.
    old_price_by_sub: dict[t.Any, Decimal] = dict(
        MembershipPayment.objects.filter(
            subscription__in=qs,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
        )
        .order_by("subscription_id", "-created_at")
        .distinct("subscription_id")
        .values_list("subscription_id", "amount")
    )

    for sub in qs:
        try:
            if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
                changed = subscription_stripe_service.update_subscription_price(sub)
                if not changed:
                    result["skipped"] += 1
                    continue
            # OFFLINE: no Stripe call. We still dispatch the notification so
            # the subscriber knows next renewal will be at the new amount.

            old_price = old_price_by_sub.get(sub.id)
            if old_price is None or old_price == new_price:
                # Skip the price-migration notification when there's no prior
                # successful payment to anchor against (would render X→X), or
                # when the subscriber already paid the new price.
                result["migrated"] += 1
                continue
            if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE and (
                _price_migration_already_notified(sub, new_price)
            ):
                # OFFLINE re-run safety: nothing on the row records the
                # migration (the last-paid-price anchor stays stale until the
                # next renewal), so dedupe on the already-sent notice.
                result["skipped"] += 1
                continue
            _dispatch_price_migration(sub, old_price=old_price, new_price=new_price)
            result["migrated"] += 1
        except Exception as exc:  # noqa: BLE001 — caught for per-sub reporting
            result["failed"] += 1
            result["errors"].append({"sub_id": str(sub.id), "reason": str(exc)})
            logger.error(
                "migrate_plan_subscribers_failed_one",
                plan_id=str(plan.pk),
                subscription_id=str(sub.pk),
                error=str(exc),
            )

    logger.info(
        "migrate_plan_subscribers_done",
        plan_id=str(plan.pk),
        initiated_by=str(initiated_by.id),
        **result,
    )
    return result


def _price_migration_already_notified(subscription: MembershipSubscription, new_price: t.Any) -> bool:
    """True when this subscriber already received the notice for this exact price change."""
    from notifications.models import Notification  # lazy: keep app import edges thin

    plan = subscription.plan
    return Notification.objects.filter(
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        context__organization_slug=subscription.organization.slug,
        context__plan_name=plan.name,
        context__new_amount=_format_money(new_price, plan.currency),
    ).exists()


# Refund handling (``refund_payment`` + full-refund auto-cancel) lives in
# :mod:`events.service.subscription_refunds` (file-length cap).


# Notification dispatch helpers (``_dispatch_*``, ``_format_money``,
# ``_common_subscription_context``) live in
# :mod:`events.service.subscription_notifications` (file-length cap). They are
# re-imported here so existing ``subscription_service._dispatch_*`` call sites
# (tasks, webhook dispatch, refunds) and test patches keep working.
from events.service.subscription_notifications import (  # noqa: E402  (post-domain import)
    _common_subscription_context as _common_subscription_context,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_cancellation_confirmed as _dispatch_cancellation_confirmed,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_payment_failed as _dispatch_payment_failed,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_price_migration as _dispatch_price_migration,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_renewal_succeeded as _dispatch_renewal_succeeded,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_revival_checkout as _dispatch_revival_checkout,
)
from events.service.subscription_notifications import (  # noqa: E402
    _dispatch_subscription_expired as _dispatch_subscription_expired,
)
from events.service.subscription_notifications import (  # noqa: E402
    _format_money as _format_money,
)
from notifications.enums import NotificationType  # noqa: E402  (post-domain import)
