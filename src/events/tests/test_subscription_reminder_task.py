"""Tests for the send_subscription_renewal_reminders Celery beat task."""

import typing as t
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.tasks import send_subscription_renewal_reminders
from events.utils.subscription_periods import REMINDER_DAYS
from notifications.enums import NotificationType
from notifications.models import Notification


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="rem_user", email="rem_user@example.com", password="pass")


@pytest.fixture
def make_sub(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    subscriber: RevelUser,
) -> t.Callable[..., MembershipSubscription]:
    def _make(
        *,
        period_end_offset_days: int,
        status: str,
        cancel_at_period_end: bool = False,
    ) -> MembershipSubscription:
        # A local-calendar-day offset, not a 24h-multiple delta: the task compares
        # ``current_period_end__date`` (rendered in settings.TIME_ZONE) against
        # ``timezone.localdate() + REMINDER_DAYS``. Anchoring at local noon keeps the
        # resulting date exact across both Europe/Vienna DST transitions.
        period_end_day = timezone.localdate() + timedelta(days=period_end_offset_days)
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=status,
            cancel_at_period_end=cancel_at_period_end,
            current_period_start=timezone.now(),
            current_period_end=timezone.make_aware(datetime.combine(period_end_day, time(12, 0))),
        )

    return _make


@pytest.mark.django_db
class TestRenewalReminderTask:
    def test_fires_at_three_days_before_period_end(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 1
        assert (
            Notification.objects.filter(notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER).count() == 1
        )

    def test_skipped_two_days_before_when_already_reminded(
        self, make_sub: t.Callable[..., MembershipSubscription]
    ) -> None:
        """Inside the window, a period already reminded is not reminded again (one reminder per charge)."""
        sub = make_sub(
            period_end_offset_days=REMINDER_DAYS - 1,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(renewal_reminded_for=sub.current_period_end)
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

    def test_unreminded_period_inside_the_window_is_caught_up(
        self, make_sub: t.Callable[..., MembershipSubscription]
    ) -> None:
        """A period ending sooner than the target (short first period, or a missed beat run) still gets one."""
        make_sub(
            period_end_offset_days=REMINDER_DAYS - 1,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        assert send_subscription_renewal_reminders()["sent"] == 1
        assert send_subscription_renewal_reminders()["sent"] == 0

    def test_period_end_nudged_by_seconds_is_not_reminded_again(
        self, make_sub: t.Callable[..., MembershipSubscription]
    ) -> None:
        """A sync that shifts current_period_end by seconds must not re-send the same reminder."""
        sub = make_sub(period_end_offset_days=REMINDER_DAYS, status=MembershipSubscription.SubscriptionStatus.ACTIVE)
        assert send_subscription_renewal_reminders()["sent"] == 1
        assert sub.current_period_end is not None
        MembershipSubscription.objects.filter(pk=sub.pk).update(
            current_period_end=sub.current_period_end + timedelta(seconds=7)
        )
        assert send_subscription_renewal_reminders()["sent"] == 0

    def test_backfill_migration_marks_periods_the_old_rule_already_reminded(
        self, make_sub: t.Callable[..., MembershipSubscription]
    ) -> None:
        """0148: periods ending before the target were reminded on their exact day under the old rule."""
        import importlib

        from django.apps import apps as django_apps

        backfill = importlib.import_module("events.migrations.0148_backfill_renewal_reminded_for").backfill
        reminded_before = make_sub(
            period_end_offset_days=REMINDER_DAYS - 1, status=MembershipSubscription.SubscriptionStatus.ACTIVE
        )
        backfill(django_apps, None)
        reminded_before.refresh_from_db()
        assert reminded_before.renewal_reminded_for == reminded_before.current_period_end
        # The deploy-day run must not re-remind it under the widened window.
        assert send_subscription_renewal_reminders()["sent"] == 0

    def test_backfill_migration_leaves_the_exact_target_day_for_the_beat(
        self, make_sub: t.Callable[..., MembershipSubscription]
    ) -> None:
        import importlib

        from django.apps import apps as django_apps

        backfill = importlib.import_module("events.migrations.0148_backfill_renewal_reminded_for").backfill
        due_today = make_sub(
            period_end_offset_days=REMINDER_DAYS, status=MembershipSubscription.SubscriptionStatus.ACTIVE
        )
        backfill(django_apps, None)
        due_today.refresh_from_db()
        assert due_today.renewal_reminded_for is None
        assert send_subscription_renewal_reminders()["sent"] == 1

    def test_skipped_four_days_before(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS + 1,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

    def test_skipped_for_cancel_at_period_end(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            cancel_at_period_end=True,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

    def test_skipped_for_paused(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

    def test_skipped_for_past_due(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

    def test_prorated_calendar_joiner_is_quoted_the_full_period_price(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """The first renewal after a prorated stub quotes the plan price, not the 2.00 stub."""
        calendar_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Calendar",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            period_alignment=MembershipSubscriptionPlan.PeriodAlignment.CALENDAR,
        )
        period_end_day = timezone.localdate() + timedelta(days=REMINDER_DAYS)
        period_end = timezone.make_aware(datetime.combine(period_end_day, time(12, 0)))
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=calendar_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_end=period_end,
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("2.00"),
            full_period_amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=period_end,
        )
        assert send_subscription_renewal_reminders()["sent"] == 1
        notification = Notification.objects.get(notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER)
        assert Decimal(notification.context["amount"].split()[0]) == Decimal("10")


@pytest.mark.django_db
class TestRenewalReminderBeforeEveryCharge:
    @pytest.fixture
    def online_calendar_plan(self, tier: MembershipTier) -> MembershipSubscriptionPlan:
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Calendar online",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            period_alignment=MembershipSubscriptionPlan.PeriodAlignment.CALENDAR,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )

    def test_short_calendar_stub_is_reminded_once_with_the_full_price(
        self,
        online_calendar_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A stub ending in hours never hits the REMINDER_DAYS date, yet its first full charge is announced."""
        period_end = timezone.now() + timedelta(hours=5)
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_calendar_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_end=period_end,
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("0.50"),
            full_period_amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=period_end,
        )
        assert send_subscription_renewal_reminders()["sent"] == 1
        notification = Notification.objects.get(notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER)
        assert Decimal(notification.context["amount"].split()[0]) == Decimal("10")
        # Re-running the beat (and running it again later in the same period) sends nothing more.
        assert send_subscription_renewal_reminders()["sent"] == 0
        assert send_subscription_renewal_reminders()["sent"] == 0
        assert (
            Notification.objects.filter(notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER).count() == 1
        )

    def test_online_rolling_sub_is_reminded_once_at_reminder_days_and_not_again(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        MembershipSubscriptionPlan.objects.filter(pk=plan.pk).update(
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE
        )
        period_end_day = timezone.localdate() + timedelta(days=REMINDER_DAYS)
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_end=timezone.make_aware(datetime.combine(period_end_day, time(12, 0))),
        )
        assert send_subscription_renewal_reminders()["sent"] == 1
        assert send_subscription_renewal_reminders()["sent"] == 0
        # The next day's beat sees the period inside the window, but it was already reminded.
        with freeze_time(timezone.now() + timedelta(days=1)):
            assert send_subscription_renewal_reminders()["sent"] == 0
        assert (
            Notification.objects.filter(notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER).count() == 1
        )
