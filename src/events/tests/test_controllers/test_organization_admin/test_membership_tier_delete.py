"""Tests for the membership-tier delete guard (#804).

Lives in its own module because ``test_members.py`` (tier CRUD) and
``test_subscriptions.py`` (plans/subscriptions) are both close to the
1000-line file limit.
"""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
)
from events.service import subscription_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Probe Tier")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="tier_delete_subscriber", email="tier-delete-sub@example.com", password="pass"
    )


def _url(organization: Organization, tier: MembershipTier) -> str:
    return reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})


def test_delete_unreferenced_tier_returns_204(
    organization_owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    """A tier nothing references deletes cleanly."""
    response = organization_owner_client.delete(_url(organization, tier))

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_tier_with_unsubscribed_plan_returns_204(
    organization_owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
) -> None:
    """Plans nobody subscribed to cascade away with the tier."""
    response = organization_owner_client.delete(_url(organization, tier))

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()
    assert not MembershipSubscriptionPlan.objects.filter(id=plan.id).exists()


def test_delete_tier_with_subscribed_plan_returns_409(
    organization_owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> None:
    """A plan carrying a subscription PROTECTs the cascade — answer 409, not 500."""
    subscription_service.create_subscription(plan, subscriber)

    response = organization_owner_client.delete(_url(organization, tier))

    assert response.status_code == 409
    assert "subscriptions" in response.json()["detail"]
    assert MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_tier_with_cancelled_subscription_returns_409(
    organization_owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> None:
    """Terminal subscriptions are still money history: they keep blocking the delete (#804 repro)."""
    subscription = subscription_service.create_subscription(plan, subscriber)
    subscription_service.cancel_subscription(subscription, immediate=True)
    plan.refresh_from_db()

    response = organization_owner_client.delete(_url(organization, tier))

    assert response.status_code == 409
    assert MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_tier_with_membership_application_returns_409(
    organization_owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    subscriber: RevelUser,
) -> None:
    """Membership applications PROTECT their tier — answer 409, not 500."""
    OrganizationMembershipRequest.objects.create(organization=organization, user=subscriber, tier=tier)

    response = organization_owner_client.delete(_url(organization, tier))

    assert response.status_code == 409
    assert "applications" in response.json()["detail"]
    assert MembershipTier.objects.filter(id=tier.id).exists()
