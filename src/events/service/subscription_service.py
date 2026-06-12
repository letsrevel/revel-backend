"""Service layer for membership subscriptions (Phase 1, OFFLINE).

Function-based service per the project's hybrid conventions. Stripe-specific
logic is intentionally absent; it lands in a separate Phase 2 module.
"""

import dataclasses
import datetime
import typing as t
from datetime import timedelta
from decimal import Decimal

import structlog
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    OrganizationMember,
)
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
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return plan
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
    non-terminal subscription there. Ensures an :class:`OrganizationMember`
    exists at the plan's tier in the same transaction.
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

    # Refuse duplicate active subscription.
    duplicate = (
        MembershipSubscription.objects.filter(organization=organization, user=user)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exists()
    )
    if duplicate:
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

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

    try:
        subscription = MembershipSubscription.objects.create(
            user=user,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
    except IntegrityError as exc:
        # The partial-unique index protects against a race where two requests
        # both pass the duplicate check above. Convert the resulting 500 to a
        # clean 400.
        raise HttpError(400, str(_("This user already has an active subscription in this organization."))) from exc

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
        MembershipSubscription.objects.select_for_update()
        .select_related("plan", "plan__tier", "organization")
        .get(pk=subscription.pk)
    )
    if subscription.is_terminal:
        return subscription

    prior_status = subscription.status
    prior_cap = subscription.cancel_at_period_end

    if (
        subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
        and subscription.stripe_subscription_id
    ):
        # Lazy import to avoid a service<->stripe-service cycle.
        from events.service import subscription_stripe_service

        subscription = subscription_stripe_service.cancel_online_subscription(subscription, immediate=immediate)
        # cancel_online_subscription mirrors local state synchronously, so we
        # can apply the same transition gates as the OFFLINE path below.
    elif immediate:
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
    else:
        if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
            raise HttpError(
                400,
                str(
                    _("Cannot schedule cancellation for a paused subscription. Resume it first, or cancel immediately.")
                ),
            )
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
def pause_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Pause a non-terminal subscription.

    For ONLINE (Stripe-managed) subscriptions, dispatches to the Stripe
    service so collection is paused on Stripe via ``pause_collection``.
    Refuses to pause an ONLINE subscription that has no linked Stripe
    record yet — pausing locally without telling Stripe would let invoices
    keep generating on the Stripe side while we believe collection is
    halted.
    """
    subscription = MembershipSubscription.objects.select_for_update().select_related("plan").get(pk=subscription.pk)
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot pause a terminal subscription.")))
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
        MembershipSubscription.objects.select_for_update()
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


def _validate_revivable(subscription: MembershipSubscription) -> None:
    """Run all revival pre-flight checks. Caller is responsible for locking."""
    if subscription.status != MembershipSubscription.SubscriptionStatus.EXPIRED:
        raise HttpError(400, str(_("Only expired subscriptions can be revived.")))

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
        raise HttpError(400, str(_("This user already has an active subscription in this organization.")))

    banned = OrganizationMember.objects.filter(
        organization=org,
        user=subscription.user,
        status=OrganizationMember.MembershipStatus.BANNED,
    ).exists()
    if banned:
        raise HttpError(403, str(_("This user is banned from the organization.")))


def revive_subscription(
    subscription: MembershipSubscription,
    *,
    initial_payment: InitialPayment | None = None,
    revived_by: RevelUser | None = None,
) -> tuple[MembershipSubscription, str | None]:
    """Revive an EXPIRED subscription within the org's revival window.

    OFFLINE flow: caller must pass ``initial_payment``. The payment is
    recorded and the subscription transitions EXPIRED → ACTIVE under a
    short ``select_for_update`` transaction.

    ONLINE flow: creates a fresh Stripe Subscription on the plan's current
    price and returns a ``client_secret`` for the member to confirm payment.
    Validation happens under a brief ``select_for_update`` lock that is
    released before the Stripe HTTP call — mirroring the pattern documented
    in ``start_online_subscription`` to avoid holding the row lock across a
    slow API call. Stripe ``idempotency_key`` keys the create call to this
    subscription's current ``expired_at`` so concurrent attempts converge.

    Returns:
        A ``(subscription, client_secret)`` tuple. ``client_secret`` is
        ``None`` for OFFLINE revivals; a Stripe PaymentIntent secret for
        ONLINE revivals.

    Refuses if:
      - subscription is not EXPIRED
      - expired_at is None (legacy data)
      - revival window has elapsed
      - org disabled revival (revival_window_days == 0)
      - user has another non-terminal subscription
      - user is BANNED
      - OFFLINE revival called without initial_payment
    """
    with transaction.atomic():
        subscription = (
            MembershipSubscription.objects.select_for_update()
            .select_related("plan", "plan__tier", "organization", "user")
            .get(pk=subscription.pk)
        )
        _validate_revivable(subscription)
        is_online = subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE

        if not is_online:
            if initial_payment is None:
                raise HttpError(400, str(_("Offline revival requires recording an initial payment.")))

            subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
            subscription.save(update_fields=["status", "updated_at"])
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

    # ONLINE branch — Stripe call runs OUTSIDE the row lock (see docstring).
    from events.service import subscription_stripe_service  # lazy: avoid cycle

    client_secret = subscription_stripe_service.create_revival_subscription(subscription)
    # Stripe call mutated and saved the subscription — refresh local state.
    subscription.refresh_from_db()
    logger.info(
        "membership_subscription_revived",
        subscription_id=str(subscription.pk),
        plan_id=str(subscription.plan_id),
        organization_id=str(subscription.organization_id),
        revived_by=str(revived_by.id) if revived_by else None,
        method="online",
    )
    return subscription, client_secret


@transaction.atomic
def change_plan(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Switch ``subscription`` to ``new_plan``.

    For ONLINE subscriptions, dispatches to the Stripe service which routes
    to either an immediate prorated upgrade or a scheduled downgrade. OFFLINE
    subscriptions perform an immediate, fee-free swap — staff are expected
    to handle any settlement off-book.

    Refuses cross-organization plan changes and currency switches in either
    path; the latter would require manual prorating against a moving FX rate
    which we do not attempt.
    """
    subscription = (
        MembershipSubscription.objects.select_for_update()
        .select_related("plan", "plan__tier", "organization", "user")
        .get(pk=subscription.pk)
    )
    if new_plan.tier.organization_id != subscription.organization_id:
        raise HttpError(400, str(_("New plan must belong to the same organization as the subscription.")))
    if not new_plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))
    if new_plan.payment_method != subscription.plan.payment_method:
        raise HttpError(
            400,
            str(_("Cannot switch between ONLINE and OFFLINE plans. Cancel and create a new subscription instead.")),
        )
    if new_plan.currency.upper() != subscription.plan.currency.upper():
        raise HttpError(400, str(_("New plan must use the same currency as the current plan.")))

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
    failure is safe — already-current subs are counted as ``skipped``.
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


def _is_full_refund_of_current_period(payment: MembershipPayment) -> bool:
    """Return True when the period covered by this payment has no remaining SUCCEEDED amount.

    Aggregates per-period totals AFTER ``refund_payment`` has flipped the row
    to REFUNDED. The current period's collected amount is the sum of all
    SUCCEEDED+REFUNDED amounts (originally-collected money); fully refunded
    means SUCCEEDED total is zero.

    Refunds against a historical period (not the subscription's current
    period_start) are bookkeeping — return False.
    """
    from django.db.models import Sum

    sub = payment.subscription
    if sub.current_period_start is None:
        return False
    if payment.period_start != sub.current_period_start:
        return False

    period_payments = MembershipPayment.objects.filter(
        subscription=sub,
        period_start=sub.current_period_start,
    )
    succeeded_total = period_payments.filter(status=MembershipPayment.PaymentStatus.SUCCEEDED).aggregate(
        s=Sum("amount")
    )["s"] or Decimal("0")
    refunded_total = period_payments.filter(status=MembershipPayment.PaymentStatus.REFUNDED).aggregate(s=Sum("amount"))[
        "s"
    ] or Decimal("0")
    return succeeded_total == Decimal("0") and refunded_total > Decimal("0")


@transaction.atomic
def refund_payment(
    payment: MembershipPayment,
    *,
    recorded_by: RevelUser | None,
    notes: str = "",
) -> MembershipPayment:
    """Mark a payment as refunded.

    If the refund fully covers the subscription's current period, also
    cancels the subscription immediately. Idempotent: re-calling for an
    already-REFUNDED payment is a no-op.
    """
    payment = MembershipPayment.objects.select_for_update().get(pk=payment.pk)
    if payment.status == MembershipPayment.PaymentStatus.REFUNDED:
        return payment
    payment.status = MembershipPayment.PaymentStatus.REFUNDED
    if notes:
        payment.notes = (payment.notes + ("\n" if payment.notes else "") + notes).strip()
    payment.save(update_fields=["status", "notes", "updated_at"])

    logger.info(
        "membership_payment_refunded",
        payment_id=str(payment.id),
        subscription_id=str(payment.subscription_id),
        recorded_by=str(recorded_by.id) if recorded_by else None,
    )

    # Phase 4: full refund of the current period auto-cancels the subscription.
    if _is_full_refund_of_current_period(payment):
        cancel_subscription(payment.subscription, immediate=True)

    return payment


# === Notification dispatch helpers ============================================
# Private helpers called by OFFLINE dispatch sites (D2), ONLINE webhook
# handlers (D3), and the renewal reminder task (E1). Each fires exactly one
# notification via notification_dispatcher.create_notification and never
# mutates subscription state.


from notifications.enums import NotificationType  # noqa: E402  (post-domain import)
from notifications.service import dispatcher as notification_dispatcher  # noqa: E402


def _format_money(amount: t.Any, currency: str) -> str:
    """Format an amount with its currency for display in notifications."""
    return f"{amount} {currency}"


def _common_subscription_context(subscription: MembershipSubscription) -> dict[str, t.Any]:
    """Base context shared by all subscription notifications.

    Includes an absolute ``organization_contact_url`` so email and Telegram
    templates render clickable links instead of relative paths that break in
    those clients.
    """
    from common.models import SiteSettings  # lazy: avoid import cycle

    org = subscription.organization
    plan = subscription.plan
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    return {
        "organization_name": org.name,
        "organization_slug": org.slug,
        "plan_name": plan.name,
        "organization_contact_url": f"{frontend_base_url}/organizations/{org.slug}/contact",
    }


def _dispatch_renewal_succeeded(
    subscription: MembershipSubscription,
    *,
    customer_portal_url: str | None = None,
) -> None:
    """Fire SUBSCRIPTION_RENEWAL_SUCCEEDED for a renewal payment."""
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_format_money(plan.price, plan.currency),
        period_end=(subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""),
    )
    if customer_portal_url is not None:
        ctx["customer_portal_url"] = customer_portal_url
    notification_dispatcher.create_notification(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED, subscription.user, ctx)


def _dispatch_payment_failed(
    subscription: MembershipSubscription,
    *,
    grace_period_end: t.Any,
    is_online: bool,
    customer_portal_url: str | None = None,
) -> None:
    """Fire SUBSCRIPTION_PAYMENT_FAILED when a renewal payment fails."""
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_format_money(plan.price, plan.currency),
        grace_period_end=grace_period_end.isoformat() if grace_period_end else "",
        is_online=is_online,
    )
    if customer_portal_url is not None:
        ctx["customer_portal_url"] = customer_portal_url
    notification_dispatcher.create_notification(NotificationType.SUBSCRIPTION_PAYMENT_FAILED, subscription.user, ctx)


def _dispatch_subscription_expired(subscription: MembershipSubscription) -> None:
    """Fire SUBSCRIPTION_EXPIRED with a revival CTA if within window."""
    from common.models import SiteSettings  # lazy: avoid import cycle

    org = subscription.organization
    revival_window_end: t.Any = None
    revival_url: str | None = None
    if subscription.expired_at and org.membership_subscription_revival_window_days > 0:
        revival_window_end = subscription.expired_at + timedelta(days=org.membership_subscription_revival_window_days)
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        revival_url = f"{frontend_base_url}/organizations/{org.slug}/subscription/revive"
    ctx = _common_subscription_context(subscription)
    ctx["expired_at"] = subscription.expired_at.isoformat() if subscription.expired_at else ""
    if revival_window_end is not None:
        ctx["revival_window_end"] = revival_window_end.isoformat()
    if revival_url is not None:
        ctx["revival_url"] = revival_url
    notification_dispatcher.create_notification(NotificationType.SUBSCRIPTION_EXPIRED, subscription.user, ctx)


def _dispatch_cancellation_confirmed(subscription: MembershipSubscription, *, immediate: bool) -> None:
    """Fire SUBSCRIPTION_CANCELLATION_CONFIRMED for cancel-now or cancel-at-period-end."""
    if immediate:
        access_ends_at = timezone.now()
    else:
        access_ends_at = subscription.current_period_end or timezone.now()
    ctx = _common_subscription_context(subscription)
    ctx.update(
        immediate=immediate,
        access_ends_at=access_ends_at.isoformat(),
    )
    notification_dispatcher.create_notification(
        NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED, subscription.user, ctx
    )


def _dispatch_price_migration(
    subscription: MembershipSubscription,
    *,
    old_price: t.Any,
    new_price: t.Any,
) -> None:
    """Fire SUBSCRIPTION_PRICE_MIGRATION_NOTICE."""
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        old_amount=_format_money(old_price, plan.currency),
        new_amount=_format_money(new_price, plan.currency),
        effective_at=(subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""),
    )
    notification_dispatcher.create_notification(
        NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE, subscription.user, ctx
    )
