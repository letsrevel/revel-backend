"""Offline-payment fields on the member-facing and admin schemas."""

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
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)

pytestmark = pytest.mark.django_db


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.membership_payment_instructions = "Pay the org by IBAN"
    organization.save()
    return organization


@pytest.fixture
def tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Open")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit="year",
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )


@pytest.fixture
def awaiting_sub(
    org: Organization, plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=nonmember_user,
        organization=org,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )


def _my_sub(user: RevelUser) -> dict[str, t.Any]:
    response = _client(user).get(reverse("api:list_my_membership_subscriptions"))
    assert response.status_code == 200, response.content
    (row,) = response.json()["results"]
    return t.cast(dict[str, t.Any], row)


def test_my_subscription_instructions_only_while_awaiting(
    awaiting_sub: MembershipSubscription, plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    # Falls back to the organization's instructions.
    assert _my_sub(nonmember_user)["payment_instructions"] == "Pay the org by IBAN"

    # The plan's own instructions win.
    plan.payment_instructions = "Pay the plan by cash"
    plan.save()
    assert _my_sub(nonmember_user)["payment_instructions"] == "Pay the plan by cash"

    # Once a payment has succeeded (and the sub is active) they disappear.
    MembershipPayment.objects.create(
        subscription=awaiting_sub,
        amount=Decimal("50.00"),
        currency="EUR",
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_start=timezone.now(),
        period_end=timezone.now() + timedelta(days=365),
    )
    awaiting_sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    awaiting_sub.save()
    assert _my_sub(nonmember_user)["payment_instructions"] is None


def test_application_exposes_plan_and_subscription_status(
    org: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
    awaiting_sub: MembershipSubscription,
    nonmember_user: RevelUser,
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=tier, plan=plan, subscription=awaiting_sub
    )
    response = _client(nonmember_user).get(reverse("api:list_membership_applications"))
    assert response.status_code == 200, response.content
    (row,) = response.json()["results"]
    assert row["plan"]["id"] == str(plan.id)
    assert row["plan"]["payment_method"] == "offline"
    assert row["subscription_status"] == "pending"


def test_admin_request_includes_plan(
    organization_owner_client: Client,
    org: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
) -> None:
    OrganizationMembershipRequest.objects.create(organization=org, user=nonmember_user, tier=tier, plan=plan)
    response = organization_owner_client.get(reverse("api:list_membership_requests", kwargs={"slug": org.slug}))
    assert response.status_code == 200, response.content
    (row,) = response.json()["results"]
    assert row["plan"] == {
        "id": str(plan.id),
        "name": "Annual",
        "price": "50.00",
        "currency": "EUR",
        "period_unit": "year",
        "period_count": plan.period_count,
        "payment_method": "offline",
    }


def test_admin_request_without_plan_has_null_plan(
    organization_owner_client: Client, org: Organization, nonmember_user: RevelUser
) -> None:
    OrganizationMembershipRequest.objects.create(organization=org, user=nonmember_user)
    response = organization_owner_client.get(reverse("api:list_membership_requests", kwargs={"slug": org.slug}))
    assert response.json()["results"][0]["plan"] is None


def test_members_filter_awaiting_payment(
    organization_owner_client: Client, org: Organization, nonmember_user: RevelUser, member_user: RevelUser
) -> None:
    OrganizationMember.objects.create(
        organization=org, user=nonmember_user, status=OrganizationMember.MembershipStatus.AWAITING_PAYMENT
    )
    OrganizationMember.objects.create(organization=org, user=member_user)
    url = reverse("api:list_organization_members", kwargs={"slug": org.slug})
    response = organization_owner_client.get(url, {"status": "awaiting_payment"})
    assert response.status_code == 200, response.content
    results = response.json()["results"]
    assert [r["user"]["email"] for r in results] == [nonmember_user.email]
