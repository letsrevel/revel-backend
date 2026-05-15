"""Service layer for membership subscriptions (Phase 1, OFFLINE).

Function-based service per the project's hybrid conventions. Stripe-specific
logic is intentionally absent; it lands in a separate Phase 2 module.
"""

import dataclasses
import datetime
import typing as t
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


# ---- Plan operations ---------------------------------------------------------


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
) -> MembershipSubscriptionPlan:
    """Create a subscription plan for a membership tier."""
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=name,
        price=price,
        currency=currency,
        period_unit=period_unit,
        period_count=period_count,
        description=description,
        is_active=is_active,
    )


@transaction.atomic
def update_plan(
    plan: MembershipSubscriptionPlan,
    **fields: t.Any,
) -> MembershipSubscriptionPlan:
    """Update a plan in-place.

    Callers pass only the fields to change; full_clean runs on save.
    """
    if not fields:
        return plan
    for field, value in fields.items():
        setattr(plan, field, value)
    plan.save(update_fields=[*fields.keys(), "updated_at"])
    return plan


@transaction.atomic
def archive_plan(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Soft-disable a plan by flipping ``is_active``."""
    if plan.is_active:
        plan.is_active = False
        plan.save(update_fields=["is_active", "updated_at"])
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
) -> MembershipPayment:
    """Record a payment and advance the subscription's billing period.

    A SUCCEEDED payment advances the period and resets PENDING/PAST_DUE to
    ACTIVE. Terminal subscriptions (CANCELLED, EXPIRED) refuse the payment
    entirely — staff must create a fresh subscription instead.

    ``occurred_at`` lets staff backfill historical payments. When set, it
    becomes the anchor for ``period_start`` / ``period_end`` and is persisted
    on the row so callers can render ``occurred_at ?? created_at`` consistently.
    """
    subscription = MembershipSubscription.objects.select_for_update().get(pk=subscription.pk)
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
        if occurred_at > now:
            raise HttpError(400, str(_("occurred_at cannot be in the future.")))
        if occurred_at < subscription.created_at:
            raise HttpError(400, str(_("occurred_at cannot predate the subscription.")))
        if (
            subscription.current_period_end
            and subscription.current_period_end < now
            and occurred_at < subscription.current_period_end
        ):
            raise HttpError(
                400,
                str(_("occurred_at cannot predate the lapsed period end of the subscription.")),
            )

    anchor = occurred_at or now

    advance = status == MembershipPayment.PaymentStatus.SUCCEEDED
    period_start = (
        subscription.current_period_end
        if (advance and subscription.current_period_end and subscription.current_period_end > anchor)
        else anchor
    )
    period_end = calculate_period_end(period_start, plan) if advance else (subscription.current_period_end or anchor)

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
    """
    subscription = MembershipSubscription.objects.select_for_update().get(pk=subscription.pk)
    if subscription.is_terminal:
        return subscription

    if immediate:
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
        return subscription

    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(
            400,
            str(_("Cannot schedule cancellation for a paused subscription. Resume it first, or cancel immediately.")),
        )

    subscription.cancel_at_period_end = True
    subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
    return subscription


@transaction.atomic
def pause_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Pause a non-terminal subscription."""
    subscription = MembershipSubscription.objects.select_for_update().get(pk=subscription.pk)
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot pause a terminal subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        return subscription
    subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


@transaction.atomic
def resume_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Resume a PAUSED subscription back to ACTIVE.

    If the period has already lapsed, the next ``record_payment`` /
    grace-expiry pass will correct the status to PAST_DUE/EXPIRED.
    """
    subscription = MembershipSubscription.objects.select_for_update().get(pk=subscription.pk)
    if subscription.status != MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Only paused subscriptions can be resumed.")))
    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


@transaction.atomic
def refund_payment(
    payment: MembershipPayment,
    *,
    recorded_by: RevelUser | None,
    notes: str = "",
) -> MembershipPayment:
    """Mark a payment as refunded.

    Record-only in MVP: it does NOT alter the subscription's status or
    period. Phase 4 may revisit this.
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
    return payment
