"""Smoke tests for django-simple-history on subscription models."""

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


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
    )


@pytest.mark.django_db
class TestSubscriptionHistory:
    def test_plan_tracks_history(self, plan: MembershipSubscriptionPlan) -> None:
        plan.price = Decimal("12.00")
        plan.save()
        assert plan.history.count() == 2
        latest, previous = plan.history.first(), plan.history.all()[1]
        assert latest.price == Decimal("12.00")
        assert previous.price == Decimal("10.00")

    def test_subscription_tracks_history(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        sub = MembershipSubscription.objects.create(user=nonmember_user, plan=plan, organization=organization)
        sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        sub.save()
        assert sub.history.count() == 2


@pytest.mark.django_db
class TestExpiredAtField:
    def test_expired_at_defaults_to_none(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        sub = MembershipSubscription.objects.create(user=nonmember_user, plan=plan, organization=organization)
        assert sub.expired_at is None

    def test_expired_at_can_be_set(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        now = timezone.now()
        sub = MembershipSubscription.objects.create(
            user=nonmember_user,
            plan=plan,
            organization=organization,
            expired_at=now,
        )
        assert sub.expired_at == now


@pytest.mark.django_db
class TestRevivalWindowField:
    def test_default_is_30(self, organization: Organization) -> None:
        assert organization.membership_subscription_revival_window_days == 30

    def test_can_be_zero(self, organization: Organization) -> None:
        organization.membership_subscription_revival_window_days = 0
        organization.save()
        organization.refresh_from_db()
        assert organization.membership_subscription_revival_window_days == 0
