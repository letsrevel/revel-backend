"""End-to-end tests for plan-bearing (paid) membership applications via /apply."""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


def _make_stripe_connected(organization: Organization) -> None:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


def _online_plan(tier: MembershipTier, name: str = "Monthly") -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=name,
        price=Decimal("5.00"),
        currency="EUR",
        period_unit="month",
        payment_method=SubscriptionPaymentMethod.ONLINE,
    )


def test_apply_with_plan_creates_plan_bearing_application(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Ungated org: a plan-bearing application lands APPROVED awaiting payment."""
    _make_stripe_connected(organization)
    plan = _online_plan(tier)
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["application"]["plan_id"] == str(plan.id)
    assert body["application"]["status"] == "approved"
    assert body["eligibility"]["next_step"] == "proceed_to_payment"


def test_apply_with_plan_and_no_tier_derives_tier_from_plan(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    _make_stripe_connected(organization)
    plan = _online_plan(tier)
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 201, response.content
    assert response.json()["application"]["tier_id"] == str(tier.id)


def test_apply_with_plan_from_another_tier_is_403(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Tier/plan mismatch blocks with PLAN_UNAVAILABLE (next_step None -> hard block)."""
    _make_stripe_connected(organization)
    other_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    plan = _online_plan(other_tier)
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 403


def test_apply_with_offline_plan_is_403(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """OFFLINE plans are staff-assigned; a self-serve plan-bearing apply hard-blocks."""
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
    )
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 403


def test_apply_with_plan_on_approval_tier_stays_pending(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    _make_stripe_connected(organization)
    tier.requires_membership_approval = True
    tier.save(update_fields=["requires_membership_approval"])
    plan = _online_plan(tier)
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["application"]["status"] == "pending"
    assert body["eligibility"]["next_step"] == "wait_for_approval"


def test_reapply_with_different_plan_updates_pending_row(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    _make_stripe_connected(organization)
    tier.requires_membership_approval = True
    tier.save(update_fields=["requires_membership_approval"])
    plan_a = _online_plan(tier, name="Monthly")
    plan_b = _online_plan(tier, name="Yearly")
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    first = client.post(url, data={"tier_id": str(tier.id), "plan_id": str(plan_a.id)}, content_type="application/json")
    assert first.status_code == 201, first.content
    second = client.post(
        url, data={"tier_id": str(tier.id), "plan_id": str(plan_b.id)}, content_type="application/json"
    )
    assert second.status_code == 201, second.content
    application = OrganizationMembershipRequest.objects.get(pk=first.json()["application"]["id"])
    assert application.plan_id == plan_b.id
    assert application.status == OrganizationMembershipRequest.Status.PENDING
