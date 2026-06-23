"""Celery task advancing membership subscriptions through their lifecycle."""

import datetime
import typing as t

import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from events.models import MembershipSubscription

logger = structlog.get_logger(__name__)


class SubscriptionExpiryCounters(t.TypedDict):
    """Telemetry counters returned by ``expire_subscriptions_past_grace``."""

    expired_immediate: int
    past_due: int
    expired_after_grace: int


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
                sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
                sub.cancelled_at = sub.cancelled_at or now
                sub.save(update_fields=["status", "cancelled_at", "updated_at"])
                counters["expired_immediate"] += 1
            else:
                sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
                sub.save(update_fields=["status", "updated_at"])
                counters["past_due"] += 1

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
            sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
            sub.cancelled_at = sub.cancelled_at or now
            sub.save(update_fields=["status", "cancelled_at", "updated_at"])
            counters["expired_after_grace"] += 1

    logger.info("expire_subscriptions_past_grace_done", **counters)
    return counters
