"""Tests for the send_subscription_renewal_reminders Celery beat task."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
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
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=status,
            cancel_at_period_end=cancel_at_period_end,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=period_end_offset_days),
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

    def test_skipped_two_days_before(self, make_sub: t.Callable[..., MembershipSubscription]) -> None:
        make_sub(
            period_end_offset_days=REMINDER_DAYS - 1,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        result = send_subscription_renewal_reminders()
        assert result["sent"] == 0

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
