"""Stripe Dashboard handles on subscriptions (organizer parity with the ticket admin).

A PENDING ONLINE subscription is not linked to a Stripe Subscription yet — only to
the Checkout Session that will create it. That is exactly the "the member says they
paid but it still shows PENDING" support case, so the admin surface must expose the
session id and a Dashboard link to it. Members never see any Stripe handle.
"""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def pending_subscription(
    online_plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
) -> MembershipSubscription:
    """A member who checked out but whose Stripe Subscription has not landed yet."""
    return MembershipSubscription.objects.create(
        user=member_user,
        plan=online_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_test_pending",
    )


class TestStripeDashboardUrl:
    """Model-level URL resolution, mirroring ``Payment.stripe_dashboard_url``."""

    def test_linked_subscription_points_at_the_subscription(self) -> None:
        sub = MembershipSubscription(stripe_subscription_id="sub_123", stripe_checkout_session_id="cs_123")
        url = sub.stripe_dashboard_url()
        assert url is not None
        assert url.startswith("https://dashboard.stripe.com/")
        assert url.endswith("/subscriptions/sub_123")

    def test_pending_falls_back_to_the_checkout_session(self) -> None:
        sub = MembershipSubscription(stripe_checkout_session_id="cs_123")
        url = sub.stripe_dashboard_url()
        assert url is not None
        assert url.startswith("https://dashboard.stripe.com/")
        assert url.endswith("/checkout/sessions/cs_123")

    def test_offline_subscription_has_no_url(self) -> None:
        assert MembershipSubscription().stripe_dashboard_url() is None


class TestAdminSurface:
    def test_detail_exposes_checkout_session_and_fallback_url(
        self,
        organization_owner_client: Client,
        organization: Organization,
        pending_subscription: MembershipSubscription,
    ) -> None:
        url = reverse(
            "api:get_subscription",
            kwargs={"slug": organization.slug, "sub_id": pending_subscription.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["stripe_subscription_id"] is None
        assert data["stripe_checkout_session_id"] == "cs_test_pending"
        assert data["stripe_dashboard_url"].endswith("/checkout/sessions/cs_test_pending")

    def test_list_exposes_checkout_session_and_fallback_url(
        self,
        organization_owner_client: Client,
        organization: Organization,
        pending_subscription: MembershipSubscription,
    ) -> None:
        url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200, response.content
        row = response.json()["results"][0]
        assert row["id"] == str(pending_subscription.id)
        assert row["stripe_checkout_session_id"] == "cs_test_pending"
        assert row["stripe_dashboard_url"].endswith("/checkout/sessions/cs_test_pending")


class TestMemberSurface:
    def test_member_view_leaks_no_stripe_handles(
        self,
        member_client: Client,
        organization: Organization,
        pending_subscription: MembershipSubscription,
    ) -> None:
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = member_client.get(url)

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["id"] == str(pending_subscription.id)
        assert "stripe_checkout_session_id" not in data
        assert "stripe_subscription_id" not in data
        assert "stripe_dashboard_url" not in data
