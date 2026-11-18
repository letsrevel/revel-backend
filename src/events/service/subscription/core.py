"""Core lifecycle primitives for membership subscriptions.

Split out of :mod:`events.service.subscription.lifecycle` so that
:mod:`subscription.stripe.checkout` (which creates the local row before
minting a Checkout Session) can import them without importing the
orchestrator back — keeping the service import graph acyclic.
``subscription.lifecycle`` (and the package ``__init__``) re-export every
public name here, so existing call sites (controllers, tasks, tests) are
unaffected.
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
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
)
from events.service import member_register_service
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.subscription.notifications import _dispatch_renewal_succeeded
from events.service.subscription.offline import (
    activate_on_first_payment,
    refuse_blacklisted_first_payment,
)
from events.service.subscription.sales import ensure_plan_sales_capacity
from events.utils.membership_access import banned_memberships, is_awaiting_payment_status, is_cancelled_status
from events.utils.subscription_payment import is_never_paid_offline
from events.utils.subscription_periods import calculate_period_end, starts_mid_period

logger = structlog.get_logger(__name__)


@dataclasses.dataclass
class InitialPayment:
    """Payload bundling the optional first payment recorded with a subscription."""

    amount: Decimal
    currency: str
    recorded_by: RevelUser
    notes: str = ""
    occurred_at: datetime.datetime | None = None


def _validate_occurred_at(
    subscription: MembershipSubscription,
    occurred_at: datetime.datetime,
    now: datetime.datetime,
    *,
    future_only: bool = False,
) -> None:
    """Reject occurred_at values that don't belong to the subscription's timeline.

    ``future_only`` is for the first payment of a just-created subscription, which
    may legitimately predate the row (a member who paid in person last month):
    only the "not in the future" rule applies to it.
    """
    if occurred_at > now:
        raise HttpError(400, str(_("occurred_at cannot be in the future.")))
    if future_only:
        return
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


def _full_period_amount(
    plan: MembershipSubscriptionPlan,
    previous_end: datetime.datetime | None,
    period_start: datetime.datetime,
    *,
    advance: bool,
) -> Decimal | None:
    """The plan's full per-period price when this payment is a prorated first calendar period, else None.

    Only a subscription's first payment (no previous period) can be prorated: that is the one
    place staff charge a fraction. A late renewal also starts mid-period but is paid in full, so
    it anchors on what was paid; a join exactly on a boundary is a full-length period.
    """
    # ponytail: the stub after a rolling -> calendar plan switch is not stamped (payments don't
    # record their plan), so MRR counts that one stub at its paid amount; record the plan on the
    # payment if that ever matters.
    first_stub = (
        advance
        and previous_end is None
        and plan.first_period_pricing == MembershipSubscriptionPlan.FirstPeriodPricing.PRORATED
        and starts_mid_period(plan, period_start)
    )
    return plan.price if first_stub else None


def _payment_period(
    plan: MembershipSubscriptionPlan,
    previous_end: datetime.datetime | None,
    anchor: datetime.datetime,
    *,
    advance: bool,
) -> tuple[datetime.datetime, datetime.datetime | None, datetime.datetime]:
    """Compute the period a payment covers when it follows a period ending at ``previous_end``.

    Returns ``(period_start, period_end, row_period_end)``: ``period_end`` is what the
    subscription's ``current_period_end`` becomes (``None`` = LIFETIME) and
    ``row_period_end`` is the NOT NULL value stored on the ledger row. Only a
    SUCCEEDED (``advance``) payment chains onto ``previous_end``.
    """
    period_start = previous_end if (advance and previous_end and previous_end > anchor) else anchor
    # ``None`` here means LIFETIME: the subscription's period never ends. The
    # ledger row still needs a non-null ``period_end``, so a lifetime payment is
    # recorded as the point event it is (period_end == period_start) while the
    # subscription's own ``current_period_end`` stays NULL.
    period_end = calculate_period_end(period_start, plan) if advance else previous_end
    row_period_end = period_end if period_end is not None else (period_start if advance else anchor)
    return period_start, period_end, row_period_end


def _refuse_lapsed_period(period_end: datetime.datetime | None, now: datetime.datetime) -> None:
    """Refuse a dated SUCCEEDED payment whose resulting period already ended.

    Otherwise the subscription would be left ACTIVE with an already-lapsed period
    (callers checking only ``status`` would grant access until the expiry beat).
    """
    if period_end is not None and period_end < now:
        raise HttpError(
            400,
            str(_("Backfilled payment would produce an already-lapsed billing period; use a more recent occurred_at.")),
        )


def _hold_member_awaiting_payment(plan: MembershipSubscriptionPlan, user: RevelUser) -> bool:
    """Make sure an OFFLINE joiner has an access row: AWAITING_PAYMENT when new or CANCELLED, else untouched.

    Returns whether the subscription now holds the member (created, reactivated, or already awaiting).
    """
    organization = plan.tier.organization
    awaiting = OrganizationMember.MembershipStatus.AWAITING_PAYMENT
    with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
        member, created = get_or_create_with_race_protection(
            OrganizationMember,
            Q(organization=organization, user=user),
            {"organization": organization, "user": user, "tier": plan.tier, "status": awaiting},
        )
        if not created and is_cancelled_status(member.status):
            member.status = awaiting
            member.tier = plan.tier
            member.save(update_fields=["status", "tier", "updated_at"])
            return True
    return created or is_awaiting_payment_status(member.status)


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
    reached. In the same transaction it materializes the member for a FREE plan
    (ACTIVE at the plan's tier). For an OFFLINE plan a new or CANCELLED member is
    held as AWAITING_PAYMENT; any other existing member keeps its status and tier
    until the first payment. An ONLINE plan creates no member here: the Stripe
    sync does once Stripe reports the subscription ``active``, which with a
    delayed-confirmation method (e.g. SEPA Direct Debit) can precede the payment
    settling (#1406).
    """
    organization = plan.tier.organization

    if not plan.is_active:
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))

    # Refuse BANNED.
    banned = banned_memberships().filter(organization=organization, user=user).exists()
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
    # ONLINE plans don't grant tier benefits up front: the ``invoice.paid`` /
    # ``customer.subscription.updated`` webhook handlers materialize the member once Stripe
    # reports the subscription active (a delayed-confirmation method can get there before
    # the payment settles, #1406).
    # OFFLINE plans hold a new or CANCELLED person as AWAITING_PAYMENT until the first recorded payment
    # (``activate_on_first_payment``); an existing member keeps their tier and status until then.
    # FREE plans have nothing to wait for, so the member is materialized ACTIVE here.
    is_free = plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.FREE
    held_member: bool | None = None
    if is_free:
        with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
            update_or_create_with_race_protection(
                OrganizationMember,
                {"organization": organization, "user": user},
                {
                    "tier": plan.tier,
                    "status": OrganizationMember.MembershipStatus.ACTIVE,
                },
            )
    elif plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE:
        held_member = _hold_member_awaiting_payment(plan, user)

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
            "held_member": held_member,
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
            occurred_at=initial_payment.occurred_at,
            is_initial=True,
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
    is_initial: bool = False,
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

    ``is_initial`` is passed only by :func:`create_subscription` for the first
    payment of a just-created subscription; see :func:`_validate_occurred_at`.

    On a **LIFETIME** plan there is no period to advance: the subscription's
    ``current_period_end`` stays NULL forever (that NULL is what keeps the
    expiry/reminder beats away from it), and since
    ``MembershipPayment.period_end`` is NOT NULL the ledger row records the
    payment as the point event it is — ``period_end == period_start``. Only
    OFFLINE lifetime plans reach here; FREE ones are refused at the controller.
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
    first_offline_payment = is_never_paid_offline(subscription)
    refuse_blacklisted_first_payment(subscription)

    if occurred_at is not None:
        _validate_occurred_at(subscription, occurred_at, now, future_only=is_initial)

    anchor = occurred_at or now

    advance = status == MembershipPayment.PaymentStatus.SUCCEEDED
    period_start, period_end, row_period_end = _payment_period(
        plan, subscription.current_period_end, anchor, advance=advance
    )

    if advance and occurred_at is not None:
        _refuse_lapsed_period(period_end, now)

    payment = MembershipPayment.objects.create(
        subscription=subscription,
        amount=amount,
        # A prorated first calendar period remembers the full per-period price it stands in for.
        full_period_amount=_full_period_amount(plan, subscription.current_period_end, period_start, advance=advance),
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
    # Set before activation: the receipt it sends to an upgrading ACTIVE member quotes the new period_end.
    subscription.current_period_start = period_start
    subscription.current_period_end = period_end

    if first_offline_payment:
        # Before the save below: the PENDING -> ACTIVE save fires sync_member_from_subscription.
        activate_on_first_payment(subscription, send_receipt=dispatch_renewal_notification and not is_initial)

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
    if dispatch_renewal_notification and not first_offline_payment and prior_status in _renewal_eligible_statuses:
        # Quote what was actually recorded: staff pick the amount (and the
        # subscriber may be grandfathered on an older price), so ``plan.price``
        # would put a figure in the receipt that nobody paid.
        _dispatch_renewal_succeeded(subscription, amount=amount, currency=currency)

    return payment
