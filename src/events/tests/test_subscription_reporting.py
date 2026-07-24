"""Tests for events.service.subscription_reporting."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_reporting


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def monthly_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )


@pytest.fixture
def annual_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("96.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR,
        period_count=1,
    )


@pytest.fixture
def make_user(django_user_model: t.Type[RevelUser]) -> t.Callable[..., RevelUser]:
    counter = {"n": 0}

    def _make() -> RevelUser:
        counter["n"] += 1
        return django_user_model.objects.create_user(
            username=f"sub_user_{counter['n']}",
            email=f"sub_user_{counter['n']}@example.com",
            password="pass",
        )

    return _make


@pytest.mark.django_db
class TestEmptyOrg:
    def test_returns_zeros(self, organization: Organization) -> None:
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["active_count"] == 0
        assert metrics["mrr"] == Decimal("0")
        assert metrics["mixed_currency_warning"] is False
        assert metrics["churn_rate_30d"] == 0.0
        assert metrics["status_breakdown"]["active"] == 0


@pytest.mark.django_db
class TestMRRNormalization:
    def test_monthly_and_annual_sum(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        annual_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        # 2 monthly @ 10.00 = 20 MRR; 1 annual @ 96.00 / 12 = 8 MRR; total 28
        for _ in range(2):
            MembershipSubscription.objects.create(
                user=make_user(),
                plan=monthly_plan,
                organization=organization,
                status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=annual_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["active_count"] == 3
        assert metrics["mrr"] == Decimal("28.00")
        assert metrics["mrr_currency"] == "EUR"
        assert metrics["mixed_currency_warning"] is False


@pytest.mark.django_db
class TestMixedCurrency:
    def test_mixed_currency_flag(
        self,
        organization: Organization,
        tier: MembershipTier,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        eur_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="EUR Monthly",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        )
        usd_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="USD Monthly",
            price=Decimal("10"),
            currency="USD",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=eur_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=usd_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mixed_currency_warning"] is True
        assert metrics["mrr_currency"] == "MIXED"
        assert metrics["mrr"] == Decimal("0")


@pytest.mark.django_db
class TestChurn:
    def test_churned_30d(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        # 1 active, 1 cancelled 5 days ago, 1 cancelled 60 days ago (excluded)
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(days=5),
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(days=60),
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["active_count"] == 1
        assert metrics["churned_30d"] == 1
        # denom = active_count (1) + churned_30d (1) = 2 → 0.5
        assert metrics["churn_rate_30d"] == 0.5


@pytest.mark.django_db
class TestMRRInexactRounding:
    def test_inexact_division_sums_correctly(
        self,
        organization: Organization,
        tier: MembershipTier,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """Three subs with period_count=3 and price=100 each should sum to
        100.00 MRR (3 * 100/3 = 100), not 99.99 from accumulated rounding."""
        plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Quarterly",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            period_count=3,
        )
        for _ in range(3):
            MembershipSubscription.objects.create(
                user=make_user(),
                plan=plan,
                organization=organization,
                status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mrr"] == Decimal("100.00")


@pytest.mark.django_db
class TestSubscriptionMetricsEndpoint:
    """Integration tests for the staff metrics endpoint."""

    @pytest.fixture
    def owner_client(self, organization_owner_user: RevelUser) -> Client:
        """JWT-authed client for the organization owner."""
        refresh = RefreshToken.for_user(organization_owner_user)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    def test_owner_can_fetch_metrics(
        self,
        owner_client: Client,
        organization: Organization,
    ) -> None:
        """Owner sees the metrics endpoint successfully."""
        url = reverse("api:get_subscription_metrics", kwargs={"slug": organization.slug})
        resp = owner_client.get(url)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert "active_count" in body
        assert "mrr" in body
        assert "status_breakdown" in body
        assert isinstance(body["status_breakdown"], dict)

    def test_member_cannot_fetch_metrics(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """A plain member must be denied (404 — org not visible to non-staff users)."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
        url = reverse("api:get_subscription_metrics", kwargs={"slug": organization.slug})
        resp = client.get(url)
        assert resp.status_code == 404
