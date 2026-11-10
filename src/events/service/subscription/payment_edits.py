"""Edit and void manual (OFFLINE) membership payments.

Staff record offline payments by hand and sometimes get them wrong. These
services correct the ledger without touching money elsewhere: no notification of
any kind is sent (an edit must not re-trigger a renewal receipt), and
simple-history captures every change. Eligibility lives in
:mod:`events.utils.membership_payment_rules`, shared with the ``can_edit`` /
``can_void`` flags the API exposes.
"""

import datetime
import typing as t
from decimal import Decimal

import structlog
from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan
from events.service.subscription.core import _payment_period, _refuse_lapsed_period, _validate_occurred_at
from events.utils.membership_payment_rules import edit_refusal, non_voided_payments, period_edit_refusal

logger = structlog.get_logger(__name__)


class PaymentChanges(t.TypedDict, total=False):
    """The fields of a payment staff may change (only keys the caller sent are present)."""

    amount: Decimal
    status: str
    occurred_at: datetime.datetime
    notes: str


def _is_lifetime(subscription: MembershipSubscription) -> bool:
    return subscription.plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.LIFETIME


def _recompute_subscription_period(subscription: MembershipSubscription) -> None:
    """Re-derive the subscription's billing period from its latest SUCCEEDED, non-voided payment.

    With such a row the subscription takes its period (NULL end on LIFETIME plans)
    and PENDING/PAST_DUE become ACTIVE, mirroring ``record_payment`` -- unless that
    period has already lapsed, in which case the status is left for the grace-expiry
    sweep to correct (same precedent as ``resume_subscription``). Without one,
    the periods are cleared and ACTIVE/PAST_DUE fall back to PENDING. PAUSED (and
    any other status) is never changed here. Membership access is untouched.

    The caller must hold the subscription row lock.
    """
    Status = MembershipSubscription.SubscriptionStatus
    latest = non_voided_payments(subscription.pk).filter(status=MembershipPayment.PaymentStatus.SUCCEEDED).first()
    update_fields = ["current_period_start", "current_period_end", "updated_at"]
    if latest is not None:
        subscription.current_period_start = latest.period_start
        subscription.current_period_end = None if _is_lifetime(subscription) else latest.period_end
        lapsed = subscription.current_period_end is not None and subscription.current_period_end < timezone.now()
        if subscription.status in (Status.PENDING, Status.PAST_DUE) and not lapsed:
            subscription.status = Status.ACTIVE
            update_fields.append("status")
            if subscription.expired_at:
                subscription.expired_at = None
                update_fields.append("expired_at")
    else:
        subscription.current_period_start = None
        subscription.current_period_end = None
        if subscription.status in (Status.ACTIVE, Status.PAST_DUE):
            subscription.status = Status.PENDING
            update_fields.append("status")
    subscription.save(update_fields=update_fields)


def _lock(payment: MembershipPayment) -> MembershipPayment:
    """Lock the subscription, then the payment (the project-wide order); return the fresh payment."""
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan")
        .get(pk=payment.subscription_id)
    )
    locked = MembershipPayment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
    locked.subscription = subscription
    return locked


def _apply_period_change(
    payment: MembershipPayment,
    subscription: MembershipSubscription,
    new_status: str,
    new_occurred_at: datetime.datetime | None,
) -> list[str]:
    """Re-place ``payment`` on the timeline as if it had been recorded with the new status/date.

    Chains onto the previous SUCCEEDED, non-voided payment exactly like ``record_payment``
    does, and returns the updated field names.
    """
    now = timezone.now()
    if new_occurred_at is not None and new_occurred_at != payment.occurred_at:
        _validate_occurred_at(subscription, new_occurred_at, now, future_only=True)
    previous = (
        non_voided_payments(subscription.pk)
        .filter(status=MembershipPayment.PaymentStatus.SUCCEEDED)
        .exclude(pk=payment.pk)
        .first()
    )
    previous_end = previous.period_end if previous is not None and not _is_lifetime(subscription) else None
    advance = new_status == MembershipPayment.PaymentStatus.SUCCEEDED
    period_start, period_end, row_period_end = _payment_period(
        subscription.plan, previous_end, new_occurred_at or payment.created_at, advance=advance
    )
    if advance:
        _refuse_lapsed_period(period_end, now)
    payment.status = new_status
    payment.occurred_at = new_occurred_at
    payment.period_start = period_start
    payment.period_end = row_period_end
    return ["status", "occurred_at", "period_start", "period_end"]


@transaction.atomic
def update_payment(
    payment: MembershipPayment,
    *,
    changes: PaymentChanges,
    recorded_by: RevelUser | None,
) -> MembershipPayment:
    """Apply staff corrections to a manual payment.

    ``amount`` and ``notes`` may change on any eligible row. A change of ``status``
    or ``occurred_at`` alters the billing period, so it additionally requires the
    row to be the subscription's latest non-voided payment on a non-terminal
    subscription; the row's period is then recomputed from the previous SUCCEEDED
    payment exactly as ``record_payment`` would have, and the subscription follows.

    Raises:
        MembershipPaymentEditRefusedError: (409) the row or subscription is ineligible.
        HttpError: (400) ``occurred_at`` is in the future, or a SUCCEEDED result
            would leave an already-lapsed billing period.
    """
    if not changes:
        return payment
    payment = _lock(payment)
    subscription = payment.subscription

    refusal = edit_refusal(payment)
    if refusal is not None:
        raise refusal

    new_status = changes.get("status", payment.status)
    new_occurred_at = changes.get("occurred_at", payment.occurred_at)
    period_change = new_status != payment.status or new_occurred_at != payment.occurred_at
    if period_change:
        refusal = period_edit_refusal(payment)
        if refusal is not None:
            raise refusal

    update_fields = ["updated_at"]
    for field in ("amount", "notes"):
        if field in changes:
            setattr(payment, field, changes[field])
            update_fields.append(field)

    if period_change:
        update_fields += _apply_period_change(payment, subscription, new_status, new_occurred_at)

    payment.save(update_fields=update_fields)
    if period_change:
        _recompute_subscription_period(subscription)

    logger.info(
        "membership_payment_edited",
        payment_id=str(payment.id),
        subscription_id=str(subscription.id),
        fields=sorted(changes),
        recorded_by=str(recorded_by.id) if recorded_by else None,
    )
    return payment


@transaction.atomic
def void_payment(
    payment: MembershipPayment,
    *,
    recorded_by: RevelUser | None,
    notes: str = "",
) -> MembershipPayment:
    """Void a manual payment entered by mistake and roll the subscription period back.

    Same eligibility as a period-affecting edit. The row is kept (status VOIDED, the
    reason appended to ``notes``) so the ledger stays auditable, but it no longer
    counts as money or as a billing period.

    Raises:
        MembershipPaymentEditRefusedError: (409) the row or subscription is ineligible.
    """
    payment = _lock(payment)
    refusal = period_edit_refusal(payment)
    if refusal is not None:
        raise refusal
    payment.status = MembershipPayment.PaymentStatus.VOIDED
    if notes:
        payment.notes = (payment.notes + ("\n" if payment.notes else "") + notes).strip()
    payment.save(update_fields=["status", "notes", "updated_at"])
    _recompute_subscription_period(payment.subscription)

    logger.info(
        "membership_payment_voided",
        payment_id=str(payment.id),
        subscription_id=str(payment.subscription_id),
        recorded_by=str(recorded_by.id) if recorded_by else None,
    )
    return payment
