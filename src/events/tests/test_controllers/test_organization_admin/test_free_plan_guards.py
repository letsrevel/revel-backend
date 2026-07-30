"""Staff-endpoint guards around FREE plans and null-price patches (#832 review).

The staff subscription endpoints guard money operations with ``== ONLINE``
refusals, which stopped meaning "OFFLINE-only" the moment FREE arrived. These
pin the FREE carve-outs, plus the PATCH ``price: null`` path that used to reach
``None <= Decimal`` and 500.
"""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    SubscriptionPaymentMethod,
)

pytestmark = pytest.mark.django_db

PeriodUnit = MembershipSubscriptionPlan.PeriodUnit


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def free_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Supporter",
        price=Decimal("0"),
        currency="EUR",
        period_unit=PeriodUnit.LIFETIME,
        payment_method=SubscriptionPaymentMethod.FREE,
    )


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=PeriodUnit.MONTH,
        payment_method=SubscriptionPaymentMethod.ONLINE,
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="free_guard_sub", email="free-guard-sub@example.com", password="pass"
    )


class TestNullPricePatch:
    """An explicit JSON ``null`` price must be a 400, never a 500."""

    def test_patch_online_plan_with_null_price_returns_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        url = reverse("api:update_subscription_plan", kwargs={"slug": organization.slug, "plan_id": online_plan.id})
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"price": None}), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        online_plan.refresh_from_db()
        assert online_plan.price == Decimal("10.00")

    def test_patch_free_plan_with_null_price_returns_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        free_plan: MembershipSubscriptionPlan,
    ) -> None:
        url = reverse("api:update_subscription_plan", kwargs={"slug": organization.slug, "plan_id": free_plan.id})
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"price": None}), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        free_plan.refresh_from_db()
        assert free_plan.price == Decimal("0")

    def test_patch_plan_with_null_period_unit_returns_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """Sibling of the null-price case: a null cadence must also 400, never 500.

        This one never crashed (the shape rules only compare ``period_unit`` for
        equality, which is None-safe) — it is pinned so the NOT NULL column
        keeps answering with a clean 400 from ``full_clean`` downstream.
        """
        url = reverse("api:update_subscription_plan", kwargs={"slug": organization.slug, "plan_id": online_plan.id})
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"period_unit": None}), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        online_plan.refresh_from_db()
        assert online_plan.period_unit == PeriodUnit.MONTH


class TestStaffFreeSubscriptionGuards:
    def test_staff_can_create_a_plain_free_subscription(
        self,
        organization_owner_client: Client,
        organization: Organization,
        free_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """The staff equivalent of the member's self-serve subscribe stays allowed."""
        url = reverse("api:create_subscription", kwargs={"slug": organization.slug})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps({"plan_id": str(free_plan.id), "user_id": str(subscriber.id)}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        subscription = MembershipSubscription.objects.get(user=subscriber, organization=organization)
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert subscription.current_period_end is None
        assert OrganizationMember.objects.filter(
            user=subscriber,
            organization=organization,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        ).exists()
        assert not MembershipPayment.objects.filter(subscription=subscription).exists()

    def test_staff_cannot_attach_an_initial_payment_to_a_free_plan(
        self,
        organization_owner_client: Client,
        organization: Organization,
        free_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        url = reverse("api:create_subscription", kwargs={"slug": organization.slug})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "plan_id": str(free_plan.id),
                    "user_id": str(subscriber.id),
                    "initial_payment_amount": "10.00",
                    "initial_payment_currency": "EUR",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert not MembershipSubscription.objects.filter(user=subscriber, organization=organization).exists()

    def test_staff_cannot_record_a_payment_against_a_free_subscription(
        self,
        organization_owner_client: Client,
        organization: Organization,
        free_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        subscription = MembershipSubscription.objects.create(
            user=subscriber,
            plan=free_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        url = reverse("api:record_subscription_payment", kwargs={"slug": organization.slug, "sub_id": subscription.id})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps({"amount": "10.00", "currency": "EUR"}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert not MembershipPayment.objects.filter(subscription=subscription).exists()
        subscription.refresh_from_db()
        # The open-ended period must survive the refused write.
        assert subscription.current_period_end is None
