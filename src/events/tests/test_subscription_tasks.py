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

    def test_expired_at_set_on_cancel_at_period_end_expiry(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        """ACTIVE sub with cancel_at_period_end=True must have expired_at stamped on expiry."""
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        _make_active_sub(plan, subscriber, period_end, cancel_at_period_end=True)

        with freeze_time("2026-05-02 13:00:00"):
            expire_subscriptions_past_grace()

        sub = MembershipSubscription.objects.get(user=subscriber)
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.expired_at is not None

    def test_expired_at_set_on_past_due_grace_expiry(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        """PAST_DUE sub that exceeds the grace window must have expired_at stamped on expiry."""
        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        sub = _make_active_sub(plan, subscriber, period_end)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.save()

        # 10 days past period_end, default grace is 7.
        with freeze_time("2026-05-11 13:00:00"):
            expire_subscriptions_past_grace()

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.expired_at is not None

    def test_offline_active_to_past_due_fires_payment_failed(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        """OFFLINE ACTIVE sub that lapses → PAST_DUE fires SUBSCRIPTION_PAYMENT_FAILED."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        _make_active_sub(plan, subscriber, period_end)

        # 2 days past period_end, well within the default 7-day grace.
        with freeze_time("2026-05-03 12:00:00"):
            expire_subscriptions_past_grace()

        assert Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        ).exists()

    def test_offline_cancel_at_period_end_expiry_fires_subscription_expired(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        """OFFLINE ACTIVE sub with cancel_at_period_end=True → EXPIRED fires SUBSCRIPTION_EXPIRED."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        _make_active_sub(plan, subscriber, period_end, cancel_at_period_end=True)

        with freeze_time("2026-05-02 13:00:00"):
            expire_subscriptions_past_grace()

        assert Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        ).exists()

    def test_offline_past_due_beyond_grace_fires_subscription_expired(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        """OFFLINE PAST_DUE sub beyond grace window → EXPIRED fires SUBSCRIPTION_EXPIRED."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        period_end = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
        sub = _make_active_sub(plan, subscriber, period_end)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.save()

        # 10 days past period_end, grace is 7.
        with freeze_time("2026-05-11 13:00:00"):
            expire_subscriptions_past_grace()

        assert Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        ).exists()

    def test_online_lapsed_does_not_fire_notification(
        self,
        organization: Organization,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """An ONLINE row entering PAST_DUE is dunned by Stripe (D3 webhook
        handlers fire the payment-failed notification); this celery task must
        not duplicate it. EXPIRED transitions DO notify (see the C1 tests
        below): local expiry is authoritative and the terminal sync guard
        silences the later Stripe-side deleted event."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_y",
            stripe_product_id="prod_y",
        )
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            cancel_at_period_end=False,
            current_period_start=timezone.now() - datetime.timedelta(days=35),
            current_period_end=timezone.now() - datetime.timedelta(days=1),
        )
        expire_subscriptions_past_grace()
        # ONLINE: zero notifications from this task (Stripe webhooks handle them in D3)
        assert not Notification.objects.filter(
            user=subscriber,
            notification_type__in=[
                NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
                NotificationType.SUBSCRIPTION_EXPIRED,
            ],
        ).exists()


class TestOnlineExpiryCancelsStripe:
    """C1 (2026-06-10 reassessment): local expiry must close the Stripe side too."""

    def _make_online_sub(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        *,
        status: MembershipSubscription.SubscriptionStatus,
        period_end: datetime.datetime,
        cancel_at_period_end: bool = False,
    ) -> MembershipSubscription:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online C1",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_c1",
            stripe_product_id="prod_c1",
        )
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=status,
            cancel_at_period_end=cancel_at_period_end,
            current_period_start=period_end - datetime.timedelta(days=30),
            current_period_end=period_end,
            stripe_subscription_id="sub_c1_online",
        )

    def test_online_past_due_beyond_grace_cancels_stripe_and_notifies(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        from notifications.enums import NotificationType
        from notifications.models import Notification

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
            period_end=timezone.now() - datetime.timedelta(days=40),
        )
        with patch("events.service.subscription_stripe_service.stripe.Subscription.cancel") as cancel_mock:
            counters = expire_subscriptions_past_grace()

        assert counters["expired_after_grace"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_c1_online"
        assert Notification.objects.filter(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_EXPIRED
        ).exists()

    def test_online_cancel_at_period_end_expiry_cancels_stripe(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            period_end=timezone.now() - datetime.timedelta(days=1),
            cancel_at_period_end=True,
        )
        with patch("events.service.subscription_stripe_service.stripe.Subscription.cancel") as cancel_mock:
            counters = expire_subscriptions_past_grace()

        assert counters["expired_immediate"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        cancel_mock.assert_called_once()

    def test_stripe_cancel_failure_does_not_fail_the_task(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        import stripe as stripe_sdk

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
            period_end=timezone.now() - datetime.timedelta(days=40),
        )
        with patch(
            "events.service.subscription_stripe_service.stripe.Subscription.cancel",
            side_effect=stripe_sdk.error.StripeError("boom"),
        ):
            counters = expire_subscriptions_past_grace()

        assert counters["expired_after_grace"] == 1
        sub.refresh_from_db()
        # Local expiry is authoritative even when the Stripe call fails;
        # the nightly reconciliation observes the divergence later.
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED


class TestReconcileStripeSubscriptions:
    """C4 (2026-06-10 reassessment): nightly Stripe→local drift repair."""

    def _make_online_sub(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        *,
        status: MembershipSubscription.SubscriptionStatus,
        stripe_id: str = "sub_reconcile",
    ) -> MembershipSubscription:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online C4",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_c4",
            stripe_product_id="prod_c4",
        )
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=status,
            stripe_subscription_id=stripe_id,
        )

    def test_missed_paid_webhook_is_repaired(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A PENDING row whose Stripe sub went active (missed webhook) is revived."""
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING
        )
        stripe_payload = {
            "id": "sub_reconcile",
            "status": "active",
            "cancel_at_period_end": False,
            "items": {
                "data": [
                    {
                        "current_period_start": 1_800_000_000,
                        "current_period_end": 1_800_000_000 + 30 * 86400,
                        "price": {"id": "price_c4"},
                    }
                ]
            },
        }
        with patch(
            "stripe.Subscription.retrieve",
            return_value=stripe_payload,
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters == {"checked": 1, "missing": 0, "errors": 0}
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_end is not None

    def test_missing_stripe_subscription_is_counted_not_fatal(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        import stripe as stripe_sdk

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING)
        with patch(
            "stripe.Subscription.retrieve",
            side_effect=stripe_sdk.error.InvalidRequestError("No such subscription", param=None),
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters == {"checked": 0, "missing": 1, "errors": 0}

    def test_old_terminal_rows_are_skipped(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Terminal rows untouched for >30 days are out of reconciliation scope."""
        from unittest.mock import patch

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.CANCELLED
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(updated_at=timezone.now() - datetime.timedelta(days=60))
        with patch("stripe.Subscription.retrieve") as retrieve_mock:
            from events.tasks.subscriptions import reconcile_stripe_subscriptions

            counters = reconcile_stripe_subscriptions()

        retrieve_mock.assert_not_called()
        assert counters == {"checked": 0, "missing": 0, "errors": 0}
