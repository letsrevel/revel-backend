"""Tests for the member-facing /me subscription endpoints."""

from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service

pytestmark = pytest.mark.django_db


def _make_stripe_connected(org: Organization) -> None:
    org.stripe_account_id = "acct_test_org"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="me_sub", email="me-sub@example.com", password="pass")


@pytest.fixture
def subscriber_client(subscriber_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(subscriber_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


@pytest.fixture
def their_subscription(plan: MembershipSubscriptionPlan, subscriber_user: RevelUser) -> MembershipSubscription:
    return subscription_service.create_subscription(plan, subscriber_user)


class TestListMySubscriptions:
    def test_returns_only_own_subscriptions(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
        nonmember_user: RevelUser,
    ) -> None:
        subscription_service.create_subscription(plan, nonmember_user)
        url = reverse("api:list_my_membership_subscriptions")
        response = subscriber_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["id"] == str(their_subscription.id)

    def test_unauthenticated_blocked(self) -> None:
        url = reverse("api:list_my_membership_subscriptions")
        response = Client().get(url)
        assert response.status_code == 401


class TestGetMyOrgSubscription:
    def test_returns_active_subscription(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.get(url)
        assert response.status_code == 200
        assert response.json()["id"] == str(their_subscription.id)

    def test_returns_404_when_no_subscription(
        self,
        subscriber_client: Client,
        organization: Organization,
    ) -> None:
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.get(url)
        assert response.status_code == 404

    def test_terminal_subscription_is_hidden(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        subscription_service.cancel_subscription(their_subscription, immediate=True)
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.get(url)
        assert response.status_code == 404


class TestSubscribeEndpoint:
    @pytest.fixture
    def online_plan(self, organization: Organization, tier: MembershipTier) -> MembershipSubscriptionPlan:
        _make_stripe_connected(organization)
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly Online",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_test",
            stripe_price_id="price_test",
        )

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_subscribe_returns_client_secret(
        self,
        mock_customer: mock.Mock,
        mock_subscription: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_x")
        mock_subscription.return_value = mock.MagicMock(
            id="sub_x", latest_invoice={"payment_intent": {"client_secret": "pi_secret"}}
        )

        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"plan_id": str(online_plan.id)}, content_type="application/json")

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["client_secret"] == "pi_secret"
        assert body["subscription"]["plan_id"] == str(online_plan.id)
        assert MembershipSubscription.objects.filter(user=subscriber_user, organization=organization).exists()

    def test_subscribe_refuses_offline_plan(
        self,
        subscriber_client: Client,
        plan: MembershipSubscriptionPlan,  # OFFLINE fixture
        organization: Organization,
    ) -> None:
        _make_stripe_connected(organization)
        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"plan_id": str(plan.id)}, content_type="application/json")
        assert response.status_code == 400

    def test_subscribe_unauthenticated_blocked(
        self,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = Client().post(url, data={"plan_id": str(online_plan.id)}, content_type="application/json")
        assert response.status_code == 401

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_subscribe_stripe_failure_rolls_back(
        self,
        mock_customer: mock.Mock,
        mock_subscription: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_x")
        mock_subscription.side_effect = stripe.error.CardError("declined", "card", "card_declined")
        url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"plan_id": str(online_plan.id)}, content_type="application/json")
        assert response.status_code == 502
        assert not MembershipSubscription.objects.filter(user=subscriber_user, organization=organization).exists()


class TestCancelMyMembershipEndpoint:
    @pytest.fixture
    def online_plan(self, organization: Organization, tier: MembershipTier) -> MembershipSubscriptionPlan:
        _make_stripe_connected(organization)
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly Online",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_test",
            stripe_price_id="price_test",
        )

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_cancel_online_routes_to_stripe(
        self,
        mock_modify: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        MembershipSubscription.objects.create(
            user=subscriber_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_to_cancel",
        )

        url = reverse("api:cancel_my_membership_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"immediate": False}, content_type="application/json")
        assert response.status_code == 200, response.content
        mock_modify.assert_called_once()
        assert response.json()["cancel_at_period_end"] is True

    def test_cancel_offline_uses_phase1_path(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        url = reverse("api:cancel_my_membership_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"immediate": False}, content_type="application/json")
        assert response.status_code == 200, response.content
        their_subscription.refresh_from_db()
        assert their_subscription.cancel_at_period_end is True

    def test_cancel_404_when_no_active(self, subscriber_client: Client, organization: Organization) -> None:
        url = reverse("api:cancel_my_membership_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"immediate": False}, content_type="application/json")
        assert response.status_code == 404


class TestChangePlanEndpoint:
    @pytest.fixture
    def online_plan(self, organization: Organization, tier: MembershipTier) -> MembershipSubscriptionPlan:
        _make_stripe_connected(organization)
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly Online",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_change",
            stripe_price_id="price_change_a",
        )

    @pytest.fixture
    def pricier_online_plan(self, tier: MembershipTier) -> MembershipSubscriptionPlan:
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Premium Online",
            price=Decimal("25.00"),
            currency="EUR",
            period_unit="month",
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_premium",
            stripe_price_id="price_premium",
        )

    @pytest.fixture
    def online_subscription(
        self,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_change_plan_test",
        )

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve")
    def test_upgrade_routes_through_stripe(
        self,
        mock_retrieve: mock.Mock,
        mock_modify: mock.Mock,
        subscriber_client: Client,
        online_subscription: MembershipSubscription,
        pricier_online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        mock_retrieve.return_value = {"items": {"data": [{"id": "si_swap"}]}}
        url = reverse("api:change_my_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(
            url, data={"plan_id": str(pricier_online_plan.id)}, content_type="application/json"
        )
        assert response.status_code == 200, response.content
        mock_modify.assert_called_once()
        assert mock_modify.call_args.kwargs["proration_behavior"] == "create_prorations"
        body = response.json()
        assert body["plan_id"] == str(pricier_online_plan.id)

    def test_change_plan_refuses_cross_currency(
        self,
        subscriber_client: Client,
        online_subscription: MembershipSubscription,
        tier: MembershipTier,
        organization: Organization,
    ) -> None:
        usd_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="USD Plan",
            price=Decimal("12.00"),
            currency="USD",
            period_unit="month",
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_usd",
            stripe_price_id="price_usd",
        )
        url = reverse("api:change_my_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={"plan_id": str(usd_plan.id)}, content_type="application/json")
        assert response.status_code == 400

    def test_change_plan_404_when_no_active(
        self,
        subscriber_client: Client,
        pricier_online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        url = reverse("api:change_my_membership_plan", kwargs={"org_id": organization.id})
        response = subscriber_client.post(
            url, data={"plan_id": str(pricier_online_plan.id)}, content_type="application/json"
        )
        assert response.status_code == 404


class TestBillingPortalEndpoint:
    @pytest.fixture
    def stripe_org(self, organization: Organization) -> Organization:
        _make_stripe_connected(organization)
        return organization

    @pytest.fixture
    def subscriber_profile(
        self,
        subscriber_user: RevelUser,
        stripe_org: Organization,
    ) -> None:
        """Seed a CustomerProfile so the user qualifies for a portal session."""
        from events.models import CustomerProfile

        CustomerProfile.objects.create(
            user=subscriber_user, organization=stripe_org, stripe_customer_id="cus_seeded_portal"
        )

    @mock.patch("events.service.subscription_stripe_service.stripe.billing_portal.Session.create")
    def test_returns_portal_url(
        self,
        mock_portal: mock.Mock,
        subscriber_client: Client,
        stripe_org: Organization,
        subscriber_profile: None,
    ) -> None:
        mock_portal.return_value = mock.MagicMock(url="https://stripe.example/portal/123")
        url = reverse("api:create_billing_portal_session", kwargs={"org_id": stripe_org.id})
        response = subscriber_client.post(
            url,
            data={"return_url": "https://app.example/billing"},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["url"] == "https://stripe.example/portal/123"
        # Pydantic's HttpUrl normalizes the URL; check the prefix instead of an exact match.
        assert mock_portal.call_args.kwargs["return_url"].startswith("https://app.example/billing")

    def test_refuses_when_no_customer_profile(
        self,
        subscriber_client: Client,
        stripe_org: Organization,
    ) -> None:
        """Strangers who never subscribed cannot trigger a portal session."""
        url = reverse("api:create_billing_portal_session", kwargs={"org_id": stripe_org.id})
        response = subscriber_client.post(url, data={}, content_type="application/json")
        assert response.status_code == 404

    def test_rejects_invalid_return_url(
        self,
        subscriber_client: Client,
        stripe_org: Organization,
        subscriber_profile: None,
    ) -> None:
        """Non-http(s) ``return_url`` is rejected at the schema layer."""
        url = reverse("api:create_billing_portal_session", kwargs={"org_id": stripe_org.id})
        response = subscriber_client.post(
            url,
            data={"return_url": "javascript:alert(1)"},
            content_type="application/json",
        )
        assert response.status_code == 422

    def test_unauthenticated_blocked(self, stripe_org: Organization) -> None:
        url = reverse("api:create_billing_portal_session", kwargs={"org_id": stripe_org.id})
        response = Client().post(url, data={}, content_type="application/json")
        assert response.status_code == 401

    def test_refuses_non_connected_org(
        self,
        subscriber_client: Client,
        organization: Organization,
    ) -> None:
        url = reverse("api:create_billing_portal_session", kwargs={"org_id": organization.id})
        response = subscriber_client.post(url, data={}, content_type="application/json")
        assert response.status_code == 400
