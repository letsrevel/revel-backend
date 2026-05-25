"""Tests for the subscription-expiry beat task."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service
from events.tasks import expire_subscriptions_past_grace

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="task_user", email="task@example.com", password="pass")


def _make_active_sub(
    plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
    period_end: datetime.datetime,
    *,
    cancel_at_period_end: bool = False,
) -> MembershipSubscription:
    sub = subscription_service.create_subscription(plan, subscriber)
    sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    sub.current_period_start = period_end - datetime.timedelta(days=30)
    sub.current_period_end = period_end
    sub.cancel_at_period_end = cancel_at_period_end
    sub.save()
    return sub


class TestExpireSubscriptions:
    def test_active_lapsed_becomes_past_due(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, organization: Organization
    ) -> None:
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        _make_active_sub(plan, subscriber, period_end)

        # 2 days past period_end, well within the default 7-day grace.
        with freeze_time("2026-05-03 12:00:00"):
            counters = expire_subscriptions_past_grace()
        assert counters["past_due"] == 1
        sub = MembershipSubscription.objects.get(user=subscriber)
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_active_lapsed_with_cancel_at_period_end_expires_immediately(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, organization: Organization
    ) -> None:
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        _make_active_sub(plan, subscriber, period_end, cancel_at_period_end=True)

        with freeze_time("2026-05-02 13:00:00"):
            counters = expire_subscriptions_past_grace()
        assert counters["expired_immediate"] == 1
        sub = MembershipSubscription.objects.get(user=subscriber)
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_past_due_within_grace_stays_past_due(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        sub = _make_active_sub(plan, subscriber, period_end)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.save()

        # 5 days past, grace is 7.
        with freeze_time("2026-05-06 12:00:00"):
            counters = expire_subscriptions_past_grace()
        assert counters["expired_after_grace"] == 0
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE

    def test_past_due_beyond_grace_expires(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, organization: Organization
    ) -> None:
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        sub = _make_active_sub(plan, subscriber, period_end)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.save()

        # 10 days past period_end, grace is 7.
        with freeze_time("2026-05-11 13:00:00"):
            counters = expire_subscriptions_past_grace()
        assert counters["expired_after_grace"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_custom_org_grace_period_is_respected(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        organization.membership_grace_period_days = 14
        organization.save(update_fields=["membership_grace_period_days"])

        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        sub = _make_active_sub(plan, subscriber, period_end)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.save()

        # 10 days past — still inside the bumped 14-day grace window.
        with freeze_time("2026-05-11 13:00:00"):
            counters = expire_subscriptions_past_grace()
        assert counters["expired_after_grace"] == 0
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE

    def test_idempotent_on_no_lapsed_subscriptions(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        # Active, period ends in the future — task should make no change.
        period_end = timezone.now() + datetime.timedelta(days=15)
        _make_active_sub(plan, subscriber, period_end)
        counters = expire_subscriptions_past_grace()
        assert counters == {"expired_immediate": 0, "past_due": 0, "expired_after_grace": 0}

    def test_processes_entire_batch_in_one_run(
        self,
        plan: MembershipSubscriptionPlan,
        django_user_model: type[RevelUser],
    ) -> None:
        """Regression for #458: every candidate row is processed in a single run.

        Builds a multi-row batch spanning all three transitions and asserts the
        whole batch is handled. The original bug streamed a server-side cursor
        and crashed once a mid-loop commit recycled the pooled backend, leaving
        later rows untouched. The pooler-specific ``InvalidCursorName`` cannot be
        reproduced without PgBouncer — the ``DISABLE_SERVER_SIDE_CURSORS``
        settings guardrail covers that — so this asserts the behavioural
        invariant: nothing in the batch is skipped.

        Run time is 2026-05-11; the default grace window is 7 days. ``recent_end``
        is lapsed but still inside grace (stays PAST_DUE), ``old_end`` is lapsed
        beyond grace (expires) — keeping the two transitions from cascading.
        """
        recent_end = datetime.datetime(2026, 5, 9, 12, 0, tzinfo=datetime.timezone.utc)
        old_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)

        def _user(n: int) -> RevelUser:
            return django_user_model.objects.create_user(
                username=f"batch_user_{n}", email=f"batch{n}@example.com", password="pass"
            )

        to_past_due = [_make_active_sub(plan, _user(i), recent_end) for i in range(2)]
        to_expired_immediate = _make_active_sub(plan, _user(2), old_end, cancel_at_period_end=True)
        to_expired_grace = []
        for i in range(3, 5):
            sub = _make_active_sub(plan, _user(i), old_end)
            sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
            sub.save()
            to_expired_grace.append(sub)

        with freeze_time("2026-05-11 13:00:00"):
            counters = expire_subscriptions_past_grace()

        assert counters == {"expired_immediate": 1, "past_due": 2, "expired_after_grace": 2}
        for sub in to_past_due:
            sub.refresh_from_db()
            assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        to_expired_immediate.refresh_from_db()
        assert to_expired_immediate.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        for sub in to_expired_grace:
            sub.refresh_from_db()
            assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
