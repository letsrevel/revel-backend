"""Tests for AcceptRequestsGate and TierAvailabilityGate."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import MembershipSubscriptionPlan, MembershipTier, Organization
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, Reasons

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


def test_org_not_accepting_requests_blocks(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    organization.accept_membership_requests = False
    organization.save(update_fields=["accept_membership_requests"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.NOT_ACCEPTING_REQUESTS)
    assert result.next_step == MembershipNextStep.REQUIRES_INVITATION


def test_org_accepting_requests_falls_through(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    organization.accept_membership_requests = True
    organization.save(update_fields=["accept_membership_requests"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True


def test_tier_belongs_to_other_org_blocks(
    user: RevelUser, organization: Organization, organization_owner_user: RevelUser
) -> None:
    organization.accept_membership_requests = True
    organization.save(update_fields=["accept_membership_requests"])
    other_org = Organization.objects.create(name="Other", slug="other-tier-availability", owner=organization_owner_user)
    foreign_tier = MembershipTier.objects.create(organization=other_org, name="X")
    service = MembershipEligibilityService(user=user, organization=organization, tier=foreign_tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.TIER_UNAVAILABLE)


def test_archived_plan_blocks(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
) -> None:
    organization.accept_membership_requests = True
    organization.save(update_fields=["accept_membership_requests"])
    plan.is_active = False
    plan.save(update_fields=["is_active"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier, plan=plan)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.PLAN_UNAVAILABLE)


def test_plan_belonging_to_other_tier_blocks(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
) -> None:
    organization.accept_membership_requests = True
    organization.save(update_fields=["accept_membership_requests"])
    other_tier = MembershipTier.objects.create(organization=organization, name="Other")
    other_plan = MembershipSubscriptionPlan.objects.create(
        tier=other_tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier, plan=other_plan)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.PLAN_UNAVAILABLE)
