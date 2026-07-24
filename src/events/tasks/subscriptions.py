"""Celery tasks for membership-subscription lifecycle and renewal reminders."""

import datetime
import typing as t

import structlog
from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from events.models import MembershipSubscription, MembershipSubscriptionPlan

logger = structlog.get_logger(__name__)


class SubscriptionExpiryCounters(t.TypedDict):
    """Telemetry counters returned by ``expire_subscriptions_past_grace``."""

    expired_immediate: int
    past_due: int
    expired_after_grace: int


def _expire_row(sub: MembershipSubscription, now: "datetime.datetime", stripe_cancel_ids: list[t.Any]) -> None:
    """Terminalize one locked subscription row: save EXPIRED, queue Stripe cancel, notify.

    Local expiry is authoritative for both payment methods: the terminal sync
    guard ignores Stripe's later ``deleted`` event, so nobody else notifies the
    member. ONLINE rows are queued for a best-effort Stripe cancel after the
    row locks are released (C1 in the 2026-06-10 reassessment).
    """
    sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
    sub.cancelled_at = sub.cancelled_at or now
    sub.expired_at = sub.expired_at or now
    sub.save(update_fields=["status", "cancelled_at", "expired_at", "updated_at"])
    if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value:
        stripe_cancel_ids.append(sub.pk)
    from events.service import subscription_service  # lazy: avoid import cycle

    subscription_service._dispatch_subscription_expired(sub)


@shared_task(name="events.expire_subscriptions_past_grace")
def expire_subscriptions_past_grace() -> SubscriptionExpiryCounters:
    """Advance membership subscriptions through their lifecycle.

    Runs daily via Celery beat (see migration 0070). Transitions:

    1. ``ACTIVE`` lapsed with ``cancel_at_period_end=True`` → ``EXPIRED``.
    2. ``ACTIVE`` lapsed otherwise → ``PAST_DUE``.
    3. ``PAST_DUE`` past the org's grace window → ``EXPIRED``.

    Each row is locked with ``select_for_update`` and its preconditions
    are re-checked inside the lock so concurrent ``record_payment`` /
    cancellation calls cannot be clobbered. Rows are processed individually
    so the ``post_save`` signal fires and syncs ``OrganizationMember``.
    """
    now = timezone.now()
    counters: SubscriptionExpiryCounters = {"expired_immediate": 0, "past_due": 0, "expired_after_grace": 0}
    # ONLINE rows terminalized in this run: their Stripe subscription must be
    # cancelled too, or Smart Retries keep dunning a member who has already
    # lost access locally — and a later retry success would pay an EXPIRED
    # row (C1 in the 2026-06-10 reassessment). Stripe calls happen after the
    # row locks are released (never hold a row lock across a network call).
    stripe_cancel_ids: list[t.Any] = []

    # 1 + 2: lapsed ACTIVE → EXPIRED (if cancel_at_period_end) else PAST_DUE.
    # list(), not .iterator(): a server-side cursor can't survive the per-row
    # commits below under PgBouncer transaction pooling (see #458).
    active_lapsed_ids = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_end__lt=now,
    ).values_list("id", flat=True)
    for sub_id in list(active_lapsed_ids):
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update()
                .select_related("plan", "plan__tier", "organization")
                .get(pk=sub_id)
            )
            # Re-check inside the lock — a concurrent record_payment may have
            # renewed the period or cancelled the subscription since the
            # snapshot was taken.
            if (
                sub.status != MembershipSubscription.SubscriptionStatus.ACTIVE
                or sub.current_period_end is None
                or sub.current_period_end >= now
            ):
                continue
            if sub.cancel_at_period_end:
                _expire_row(sub, now, stripe_cancel_ids)
                counters["expired_immediate"] += 1
            else:
                sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
                sub.save(update_fields=["status", "updated_at"])
                counters["past_due"] += 1
                if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE.value:
                    from events.service import subscription_service  # lazy: avoid import cycle

                    grace_period_end = sub.current_period_end + datetime.timedelta(
                        days=sub.organization.membership_grace_period_days
                    )
                    subscription_service._dispatch_payment_failed(
                        sub,
                        grace_period_end=grace_period_end,
                        is_online=False,
                    )

    # 3: PAST_DUE past grace → EXPIRED (list() not .iterator(), see #458).
    past_due_ids = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        current_period_end__isnull=False,
    ).values_list("id", flat=True)
    for sub_id in list(past_due_ids):
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update()
                .select_related("plan", "plan__tier", "organization")
                .get(pk=sub_id)
            )
            if sub.status != MembershipSubscription.SubscriptionStatus.PAST_DUE or sub.current_period_end is None:
                continue
            grace_days = sub.organization.membership_grace_period_days
            if sub.current_period_end + datetime.timedelta(days=grace_days) >= now:
                continue
            _expire_row(sub, now, stripe_cancel_ids)
            counters["expired_after_grace"] += 1

    if stripe_cancel_ids:
        from events.service import subscription_stripe_service  # lazy: avoid import cycle

        for sub in MembershipSubscription.objects.filter(pk__in=stripe_cancel_ids).select_related("organization"):
            subscription_stripe_service.cancel_stripe_subscription_best_effort(sub, reason="local_grace_expiry")

    logger.info(
        "expire_subscriptions_past_grace_done",
        stripe_cancelled=len(stripe_cancel_ids),
        **counters,
    )
    return counters


@shared_task(name="events.send_subscription_renewal_reminders")
def send_subscription_renewal_reminders() -> dict[str, int]:
    """Fire SUBSCRIPTION_RENEWAL_REMINDER for subscriptions renewing in REMINDER_DAYS.

    Runs daily via Celery beat (see migration 0078). Processes only ACTIVE
    subscriptions whose ``current_period_end`` falls exactly REMINDER_DAYS from
    today and have ``cancel_at_period_end=False`` (no point reminding about a
    subscription already scheduled to end).

    Idempotency: the date-equality check naturally fires each subscription
    exactly once per period. A missed day (Celery downtime) means missed
    reminders that day — acceptable for a non-critical nudge.

    Returns:
        Counters dict: {"sent": N}.
    """
    from events.service import subscription_service  # lazy: avoid import cycle
    from events.utils.subscription_periods import REMINDER_DAYS
    from notifications.enums import NotificationType
    from notifications.service import dispatcher as notification_dispatcher

    today = timezone.now().date()
    target_date = today + datetime.timedelta(days=REMINDER_DAYS)
    qs = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        cancel_at_period_end=False,
        current_period_end__date=target_date,
    ).select_related("plan", "organization", "user")
    sent = 0
    for sub in qs.iterator():
        plan = sub.plan
        ctx = subscription_service._common_subscription_context(sub)
        ctx.update(
            amount=subscription_service._format_money(plan.price, plan.currency),
            period_end=sub.current_period_end.date().isoformat() if sub.current_period_end else "",
            is_online=(plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value),
        )
        notification_dispatcher.create_notification(NotificationType.SUBSCRIPTION_RENEWAL_REMINDER, sub.user, ctx)
        sent += 1
    logger.info("send_subscription_renewal_reminders_done", sent=sent)
    return {"sent": sent}


class SubscriptionReconcileCounters(t.TypedDict):
    """Telemetry counters returned by ``reconcile_stripe_subscriptions``."""

    checked: int
    missing: int
    errors: int


@shared_task(name="events.reconcile_stripe_subscriptions")
def reconcile_stripe_subscriptions() -> SubscriptionReconcileCounters:
    """Nightly drift repair: re-observe Stripe state for ONLINE subscriptions.

    Webhook delivery is best-effort; a missed event leaves app-truth and
    billing-truth diverged until the next event happens to arrive (C4 in the
    2026-06-10 reassessment: paid-but-no-access, phantom pending plan changes,
    unpaid access until the local grace clock). This task closes the loop by
    retrieving each relevant Stripe Subscription and feeding the payload
    through :func:`sync_subscription_from_stripe` — the same idempotent,
    diff-based mirror the webhooks use.

    Scope: every non-terminal row with a ``stripe_subscription_id``, plus
    recently-updated terminal rows (their Stripe side may still be dunning —
    the terminal sync guard keeps them frozen locally, but observing them logs
    the divergence). The Stripe retrieve happens OUTSIDE the per-row
    transaction; only the sync holds the row lock (see #458 for why no
    ``.iterator()`` + per-row commits).
    """
    import stripe as stripe_sdk

    from events.service import subscription_stripe_service
    from events.service.subscription_stripe_payloads import _stripe_account_kwargs

    now = timezone.now()
    counters: SubscriptionReconcileCounters = {"checked": 0, "missing": 0, "errors": 0}

    candidate_ids = list(
        MembershipSubscription.objects.filter(
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        .exclude(stripe_subscription_id="")
        .filter(
            ~models.Q(status__in=MembershipSubscription.TERMINAL_STATUSES)
            | models.Q(updated_at__gte=now - datetime.timedelta(days=30))
        )
        .values_list("id", flat=True)
    )

    for sub_id in candidate_ids:
        sub = MembershipSubscription.objects.select_related("organization", "plan").filter(pk=sub_id).first()
        if sub is None or not sub.stripe_subscription_id:
            continue
        try:
            stripe_sub = stripe_sdk.Subscription.retrieve(
                sub.stripe_subscription_id,
                **_stripe_account_kwargs(sub.organization),
            )
        except stripe_sdk.error.InvalidRequestError:
            # resource_missing: Stripe has no such subscription (test-mode
            # wipe, manual deletion). Nothing to mirror; surface it.
            counters["missing"] += 1
            logger.warning(
                "subscription_reconcile_stripe_missing",
                subscription_id=str(sub_id),
                stripe_subscription_id=sub.stripe_subscription_id,
            )
            continue
        except stripe_sdk.error.StripeError:
            counters["errors"] += 1
            logger.exception(
                "subscription_reconcile_retrieve_failed",
                subscription_id=str(sub_id),
                stripe_subscription_id=sub.stripe_subscription_id,
            )
            continue

        with transaction.atomic():
            subscription_stripe_service.sync_subscription_from_stripe(dict(stripe_sub))
        counters["checked"] += 1

    logger.info("reconcile_stripe_subscriptions_done", **counters)
    return counters
