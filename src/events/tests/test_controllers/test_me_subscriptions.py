"""Tests for the member-facing /me subscription endpoints."""

from decimal import Decimal

import pytest
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


class TestMySubscriptionOrgMetadata:
    def test_response_includes_organization_name_and_slug(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        url = reverse("api:get_my_organization_subscription", kwargs={"org_id": organization.id})
        response = subscriber_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["organization_name"] == organization.name
        assert data["organization_slug"] == organization.slug
        # Without an uploaded logo, the URL should be null.
        assert data["organization_logo_url"] is None

    def test_list_includes_organization_metadata(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        url = reverse("api:list_my_membership_subscriptions")
        response = subscriber_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        item = data["results"][0]
        assert item["organization_name"] == organization.name
        assert item["organization_slug"] == organization.slug
        assert "organization_logo_url" in item


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
