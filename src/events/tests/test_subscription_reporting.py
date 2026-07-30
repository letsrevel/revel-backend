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
class TestNewSubscribers:
    def test_counts_by_created_at_including_churned(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """Acquisition anchors on created_at: a subscriber who already churned still counts."""
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        churned = MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now(),
        )
        assert churned.created_at >= timezone.now() - timedelta(days=30)

        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["new_subscribers_30d"] == 2

    def test_excludes_never_started_pending_rows(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """A mid-checkout (possibly abandoned) PENDING row has not acquired anyone."""
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["new_subscribers_30d"] == 0


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
class TestMRRGrandfathering:
    def test_grandfathered_sub_contributes_paid_amount_not_plan_price(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """A subscriber whose last SUCCEEDED payment differs from the current
        plan price (grandfathered) contributes their PAID amount to MRR."""
        from events.models import MembershipPayment

        sub = MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Paid 7.00 historically; plan was later bumped to 10.00.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("7.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now() + timedelta(days=1),
        )
        assert monthly_plan.price == Decimal("10.00")

        metrics = subscription_reporting.get_organization_metrics(organization)
        # MRR reflects the 7.00 they actually pay, not the current 10.00 plan price.
        assert metrics["mrr"] == Decimal("7.00")

    def test_falls_back_to_plan_price_without_payment(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """A sub with no SUCCEEDED payment (e.g. brand-new) uses the plan price."""
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mrr"] == Decimal("10.00")

    def test_annual_grandfathered_amount_is_normalized_monthly(
        self,
        organization: Organization,
        annual_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """The paid amount is normalized by the plan's period (annual → /12)."""
        from events.models import MembershipPayment

        sub = MembershipSubscription.objects.create(
            user=make_user(),
            plan=annual_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Old annual price of 60.00 → 5.00/month, vs current plan 96.00 → 8.00/month.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("60.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now() + timedelta(days=335),
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mrr"] == Decimal("5.00")


@pytest.mark.django_db
class TestMRRProrationInvoices:
    """A mid-cycle upgrade bills a proration delta, which is not a per-period price."""

    def test_proration_row_is_not_the_mrr_anchor(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """The latest full-period payment wins over a newer proration invoice."""
        from events.models import MembershipPayment

        sub = MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("20.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now() + timedelta(days=1),
            raw_response={"billing_reason": "subscription_cycle"},
        )
        # Mid-cycle upgrade: Stripe issues a small proration invoice.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("5.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=timezone.now() + timedelta(days=1),
            raw_response={"billing_reason": "subscription_update"},
        )

        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mrr"] == Decimal("20.00")

    def test_only_proration_payment_falls_back_to_plan_price(
        self,
        organization: Organization,
        monthly_plan: MembershipSubscriptionPlan,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """With no full-period payment to anchor on, ``plan.price`` is used."""
        from events.models import MembershipPayment

        sub = MembershipSubscription.objects.create(
            user=make_user(),
            plan=monthly_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("2.50"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=timezone.now() + timedelta(days=1),
            raw_response={"billing_reason": "subscription_update"},
        )

        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mrr"] == monthly_plan.price


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

    def test_lifetime_plan_in_another_currency_does_not_trigger_mixed(
        self,
        organization: Organization,
        tier: MembershipTier,
        make_user: t.Callable[..., RevelUser],
    ) -> None:
        """A LIFETIME plan contributes 0 MRR, so its currency must not get a vote.

        Otherwise a single USD one-off membership flips an all-EUR org to MIXED
        and zeroes out its real, recurring MRR.
        """
        eur_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="EUR Monthly",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        )
        usd_lifetime_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="USD Lifetime",
            price=Decimal("500"),
            currency="USD",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.LIFETIME,
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=eur_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        MembershipSubscription.objects.create(
            user=make_user(),
            plan=usd_lifetime_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        metrics = subscription_reporting.get_organization_metrics(organization)
        assert metrics["mixed_currency_warning"] is False
        assert metrics["mrr_currency"] == "EUR"
        assert metrics["mrr"] == Decimal("10.00")


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
