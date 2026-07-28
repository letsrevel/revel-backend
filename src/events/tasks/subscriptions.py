"""Celery tasks for membership-subscription lifecycle and renewal reminders."""

import datetime
import typing as t

import structlog
from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan

if t.TYPE_CHECKING:
    from events.service.subscription_service import MigrationResult
    from events.service.subscription_stripe_service import FeeResyncCounters

logger = structlog.get_logger(__name__)


class SubscriptionExpiryCounters(t.TypedDict):
    """Telemetry counters returned by ``expire_subscriptions_past_grace``."""

    cancelled_at_period_end: int
    past_due: int
    expired_after_grace: int


def _expire_row(sub: MembershipSubscription, now: "datetime.datetime", stripe_cancel_ids: list[t.Any]) -> None:
    """Terminalize one lapsed row as EXPIRED: save, queue Stripe cancel, notify.

    Used for genuine, involuntary lapses (a PAST_DUE subscription past its grace
    window). Local expiry is authoritative for both payment methods: the terminal
    sync guard ignores Stripe's later ``deleted`` event, so nobody else notifies
    the member. ONLINE rows are queued for a best-effort Stripe cancel after the
    row locks are released (C1 in the 2026-06-10 reassessment). ``expired_at`` is
    stamped so the row is eligible for the revival flow.
    """
    sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
    sub.cancelled_at = sub.cancelled_at or now
    sub.expired_at = sub.expired_at or now
    sub.save(update_fields=["status", "cancelled_at", "expired_at", "updated_at"])
    if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value:
        stripe_cancel_ids.append(sub.pk)
    from events.service import subscription_service  # lazy: avoid import cycle

    subscription_service._dispatch_subscription_expired(sub)


def _terminalize_cancelled_row(
    sub: MembershipSubscription, now: "datetime.datetime", stripe_cancel_ids: list[t.Any]
) -> None:
    """Terminalize a lapsed ACTIVE row the member CHOSE to cancel (cancel_at_period_end).

    Status → CANCELLED, not EXPIRED: the member opted out at the period
    boundary, so this is not an involuntary lapse to offer a "revive" CTA for.
    We stamp ``cancelled_at`` but deliberately leave ``expired_at`` unset —
    :func:`subscription_service.revive_subscription` only accepts EXPIRED rows,
    so a CANCELLED row is naturally out of the revival window (correct for a
    chosen cancel). No notification is dispatched: CANCELLATION_CONFIRMED
    already fired when the member scheduled the cancel. This closes the race
    where the beat task beats Stripe's ``customer.subscription.deleted`` webhook
    and would otherwise send "your subscription expired — revive?" on top of the
    cancellation-confirmed the member already received. ONLINE rows are queued
    for a best-effort Stripe cancel so Smart Retries stop dunning them.
    """
    sub.status = MembershipSubscription.SubscriptionStatus.CANCELLED
    sub.cancelled_at = sub.cancelled_at or now
    sub.save(update_fields=["status", "cancelled_at", "updated_at"])
    if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value:
        stripe_cancel_ids.append(sub.pk)


@shared_task(name="events.expire_subscriptions_past_grace")
def expire_subscriptions_past_grace() -> SubscriptionExpiryCounters:
    """Advance membership subscriptions through their lifecycle.

    Runs daily via Celery beat (see migration 0070). Transitions:

    1. ``ACTIVE`` lapsed with ``cancel_at_period_end=True`` → ``CANCELLED``
       (member chose to cancel; no expiry/revival notification).
    2. ``ACTIVE`` lapsed otherwise → ``PAST_DUE``.
    3. ``PAST_DUE`` past the org's grace window → ``EXPIRED``.

    Each row is locked with ``select_for_update`` and its preconditions
    are re-checked inside the lock so concurrent ``record_payment`` /
    cancellation calls cannot be clobbered. Rows are processed individually
    so the ``post_save`` signal fires and syncs ``OrganizationMember``.
    """
    now = timezone.now()
    counters: SubscriptionExpiryCounters = {"cancelled_at_period_end": 0, "past_due": 0, "expired_after_grace": 0}
    # ONLINE rows terminalized in this run: their Stripe subscription must be
    # cancelled too, or Smart Retries keep dunning a member who has already
    # lost access locally — and a later retry success would pay an EXPIRED
    # row (C1 in the 2026-06-10 reassessment). Stripe calls happen after the
    # row locks are released (never hold a row lock across a network call).
    stripe_cancel_ids: list[t.Any] = []

    # 1 + 2: lapsed ACTIVE → CANCELLED (if cancel_at_period_end) else PAST_DUE.
    # list(), not .iterator(): a server-side cursor can't survive the per-row
    # commits below under PgBouncer transaction pooling (see #458).
    active_lapsed_ids = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_end__lt=now,
    ).values_list("id", flat=True)
    for sub_id in list(active_lapsed_ids):
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update(of=("self",))
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
                _terminalize_cancelled_row(sub, now, stripe_cancel_ids)
                counters["cancelled_at_period_end"] += 1
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
                MembershipSubscription.objects.select_for_update(of=("self",))
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


class SubscriptionReminderCounters(t.TypedDict):
    """Telemetry counters returned by ``send_subscription_renewal_reminders``."""

    sent: int


@shared_task(name="events.send_subscription_renewal_reminders")
def send_subscription_renewal_reminders() -> SubscriptionReminderCounters:
    """Fire SUBSCRIPTION_RENEWAL_REMINDER for subscriptions renewing in REMINDER_DAYS.

    Runs daily via Celery beat (see migration 0102). Processes only ACTIVE
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
    from notifications.signals import notification_requested

    # localdate(), not now().date(): the ``__date`` lookup below renders as
    # ``current_period_end AT TIME ZONE <settings.TIME_ZONE>)::date``, so the
    # target must be a *local* calendar date too. Deriving it from the UTC date
    # shifts the whole cohort by a day whenever the run happens while the UTC
    # and local dates disagree — members would be reminded 2 (or 4) days out,
    # and a day's cohort can be skipped entirely if the skew flips between runs.
    today = timezone.localdate()
    target_date = today + datetime.timedelta(days=REMINDER_DAYS)
    # list(), not .iterator(): the signal handler INSERTs a Notification per row,
    # and a server-side cursor can't survive per-row commits under PgBouncer
    # transaction pooling (see #458).
    subs = list(
        MembershipSubscription.objects.filter(
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            cancel_at_period_end=False,
            current_period_end__date=target_date,
        ).select_related("plan", "organization", "user")
    )
    sent = 0
    for sub in subs:
        plan = sub.plan
        ctx = subscription_service._common_subscription_context(sub)
        ctx.update(
            amount=subscription_service._format_money(plan.price, plan.currency),
            period_end=sub.current_period_end.date().isoformat() if sub.current_period_end else "",
            is_online=(plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value),
        )
        notification_requested.send(
            sender=MembershipSubscription,
            user=sub.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER,
            context=ctx,
        )
        sent += 1
    logger.info("send_subscription_renewal_reminders_done", sent=sent)
    return SubscriptionReminderCounters(sent=sent)


@shared_task(name="events.migrate_plan_subscribers")
def migrate_plan_subscribers(plan_id: str, initiated_by_id: str) -> "MigrationResult":
    """Force-migrate a plan's non-terminal subscribers to its current price (async).

    Wraps :func:`events.service.subscription_service.migrate_plan_subscribers`,
    which issues one Stripe retrieve+modify per ONLINE subscriber — too slow to
    run inside the admin request (a large plan blows the gunicorn timeout).
    Dispatched from the migrate-subscribers endpoint via ``transaction.on_commit``
    so the worker sees the plan's committed price.

    Completion signalling is via structured logs: the service logs the aggregate
    (``migrate_plan_subscribers_done``) and each per-sub failure; the aggregate
    result dict is also returned as the Celery result. No in-app notification is
    sent (there is no fitting notification type, and adding one — enum, templates,
    preferences — is out of proportion for a staff-triggered batch job).
    """
    from accounts.models import RevelUser
    from events.service import subscription_service

    empty: MigrationResult = {"migrated": 0, "skipped": 0, "failed": 0, "errors": []}
    plan = MembershipSubscriptionPlan.objects.select_related("tier__organization").filter(pk=plan_id).first()
    if plan is None:
        logger.warning("migrate_plan_subscribers_task_plan_missing", plan_id=plan_id)
        return empty
    initiated_by = RevelUser.objects.filter(pk=initiated_by_id).first()
    if initiated_by is None:
        logger.warning("migrate_plan_subscribers_task_user_missing", plan_id=plan_id, initiated_by_id=initiated_by_id)
        return empty
    return subscription_service.migrate_plan_subscribers(plan, initiated_by=initiated_by)


class SubscriptionReconcileCounters(t.TypedDict):
    """Telemetry counters returned by ``reconcile_stripe_subscriptions``."""

    checked: int
    missing: int
    errors: int
    stale_pending_cleared: int
    ledger_backfilled: int


@shared_task(name="events.reconcile_stripe_subscriptions")
def reconcile_stripe_subscriptions() -> SubscriptionReconcileCounters:
    """Nightly drift repair: re-observe Stripe state for ONLINE subscriptions.

    Runs daily via Celery beat (see migration 0103).

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

    Two additional repairs ride along:

    - **Stale-PENDING sweep**: an abandoned ONLINE checkout leaves a PENDING
      row (no ``stripe_subscription_id``) that holds a ``max_subscriptions``
      cap slot. The ``checkout.session.expired`` webhook normally clears it;
      this sweep backstops a dropped delivery, clearing rows untouched for a
      day (sessions expire within ``PAYMENT_DEFAULT_EXPIRY_MINUTES``).
    - **Ledger backfill**: mirroring status/period repairs *access* but not
      the payment ledger. When the retrieved subscription's latest invoice is
      paid and unknown locally (its ``invoice.paid`` was dropped beyond
      Stripe's retry window), record it — revenue and the platform-fee/VAT
      decomposition feed fee invoicing and referral payouts. Known invoice
      ids are skipped so a REFUNDED ledger row is never resurrected.
    """
    import stripe as stripe_sdk

    from events.service import subscription_stripe_sync
    from events.service.subscription_stripe_payloads import _stripe_account_kwargs
    from events.service.subscription_stripe_service import _clear_stale_pending_checkout

    now = timezone.now()
    counters: SubscriptionReconcileCounters = {
        "checked": 0,
        "missing": 0,
        "errors": 0,
        "stale_pending_cleared": 0,
        "ledger_backfilled": 0,
    }

    stale_pending_ids = list(
        MembershipSubscription.objects.filter(
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            updated_at__lt=now - datetime.timedelta(days=1),
        )
        .filter(models.Q(stripe_subscription_id="") | models.Q(stripe_subscription_id__isnull=True))
        .values_list("id", flat=True)
    )
    for sub_id in stale_pending_ids:
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update()
                .filter(
                    pk=sub_id,
                    status=MembershipSubscription.SubscriptionStatus.PENDING,
                )
                .first()
            )
            # Re-check inside the lock: a resumed checkout or a racing
            # ``checkout.session.completed`` may have progressed the row.
            if sub is None or sub.stripe_subscription_id:
                continue
            _clear_stale_pending_checkout(sub)
            counters["stale_pending_cleared"] += 1
            logger.info("subscription_reconcile_stale_pending_cleared", subscription_id=str(sub_id))

    candidate_ids = list(
        MembershipSubscription.objects.filter(
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        # No Stripe Subscription to observe yet: a PENDING row mid-hosted-
        # checkout carries only a stripe_checkout_session_id — the Stripe
        # Subscription is created at session completion. Skip those.
        .exclude(stripe_subscription_id="")
        .exclude(stripe_subscription_id__isnull=True)
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
                expand=["latest_invoice"],
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
            subscription_stripe_sync.sync_subscription_from_stripe(dict(stripe_sub))
        counters["checked"] += 1

        # Ledger backfill: a paid invoice we have no row for means its
        # ``invoice.paid`` was lost for good (redelivery exhausted). The
        # existence check keeps this a strict backfill — an already-known
        # invoice (SUCCEEDED or REFUNDED) is never touched.
        latest_invoice = stripe_sub.get("latest_invoice")
        if (
            isinstance(latest_invoice, dict)
            and latest_invoice.get("id")
            and latest_invoice.get("status") == "paid"
            and not MembershipPayment.objects.filter(stripe_invoice_id=latest_invoice["id"]).exists()
        ):
            subscription_stripe_sync.record_stripe_payment_from_invoice(dict(latest_invoice), succeeded=True)
            counters["ledger_backfilled"] += 1
            logger.info(
                "subscription_reconcile_ledger_backfilled",
                subscription_id=str(sub_id),
                stripe_invoice_id=latest_invoice["id"],
            )

    logger.info("reconcile_stripe_subscriptions_done", **counters)
    return counters


@shared_task(name="events.resync_org_subscription_fees")
def resync_org_subscription_fees(org_id: str) -> "FeeResyncCounters":
    """Resync ``application_fee_percent`` on an org's live Stripe subscriptions.

    Dispatched by the :mod:`events.service.vies_service` wrappers whenever an
    org's VAT-status change moves its effective fee percent (the VAT gross-up
    frozen into each Stripe Subscription at Checkout). Per-subscription Stripe
    failures are logged and counted by the service, not raised — re-running
    is idempotent, and the ``resync_subscription_fees`` management command
    covers stragglers (including schedule-managed rows, which are skipped
    here so a pending downgrade is never dropped).
    """
    from events.models import Organization
    from events.service.subscription_stripe_service import resync_subscription_application_fees

    org = Organization.objects.get(pk=org_id)
    return resync_subscription_application_fees(org)
