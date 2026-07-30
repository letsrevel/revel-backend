"""Core lifecycle primitives for membership subscriptions.

Split out of :mod:`events.service.subscription_service` so that
:mod:`subscription_stripe_service` (which creates the local row before
minting a Checkout Session) can import them without importing the
orchestrator back — keeping the service import graph acyclic.
``subscription_service`` re-exports every public name here, so existing
call sites (controllers, tasks, tests) are unaffected.
"""

import dataclasses
import datetime
from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection, update_or_create_with_race_protection
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
)
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.subscription_notifications import _dispatch_renewal_succeeded
from events.service.subscription_sales import ensure_plan_sales_capacity
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
    # OFFLINE and FREE plans have no Stripe payment to wait for, so the member
    # is materialized here — for FREE that is the *only* place it can happen.
    is_free = plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.FREE
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        update_or_create_with_race_protection(
            OrganizationMember,
            {"organization": organization, "user": user},
            {
                "tier": plan.tier,
                "status": OrganizationMember.MembershipStatus.ACTIVE,
            },
        )

    # The partial-unique index protects against a race where two requests both
    # pass the duplicate check above; the helper re-fetches the winner whether
    # the race surfaces as IntegrityError (INSERT) or ValidationError
    # (TimeStampedModel.save's full_clean sees the committed racing row).
    #
    # A FREE plan has nothing to pay and (being LIFETIME by construction)
    # nothing to renew, so the row is live the moment it exists: ACTIVE with an
    # open-ended period. ``current_period_end`` stays NULL forever, which is
    # what keeps every expiry/reminder beat from ever selecting it.
    subscription, created = get_or_create_with_race_protection(
        MembershipSubscription,
        Q(user=user, organization=organization) & ~Q(status__in=MembershipSubscription.TERMINAL_STATUSES),
        defaults={
            "user": user,
            "plan": plan,
            "organization": organization,
            "status": (
                MembershipSubscription.SubscriptionStatus.ACTIVE
                if is_free
                else MembershipSubscription.SubscriptionStatus.PENDING
            ),
            "current_period_start": timezone.now() if is_free else None,
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
    entirely — staff must create a fresh subscription instead. ``currency``
    must match the plan's currency (we never convert between currencies).

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
    if currency.upper() != plan.currency.upper():
        # Reporting reads the currency off the plan (metrics/MRR) *and* off the
        # payment row (revenue report), so a mismatched row silently corrupts
        # both — and no FX conversion exists to reconcile them afterwards.
        raise HttpError(400, str(_("Payment currency must match the plan's currency.")))
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
    # ``None`` here means LIFETIME: the subscription's period never ends. The
    # ledger row still needs a non-null ``period_end``, so a lifetime payment is
    # recorded as the point event it is (period_end == period_start) while the
    # subscription's own ``current_period_end`` stays NULL.
    period_end = calculate_period_end(period_start, plan) if advance else subscription.current_period_end
    row_period_end = period_end if period_end is not None else (period_start if advance else anchor)

    if advance and period_end is not None and occurred_at is not None and period_end < now:
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
        period_end=row_period_end,
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
        # Quote what was actually recorded: staff pick the amount (and the
        # subscriber may be grandfathered on an older price), so ``plan.price``
        # would put a figure in the receipt that nobody paid.
        _dispatch_renewal_succeeded(subscription, amount=amount, currency=currency)

    return payment
