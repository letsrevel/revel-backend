"""Tests for AlreadyMemberGate."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, Reasons

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])


@pytest.fixture
def tier_a(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="A")


@pytest.fixture
def tier_b(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="B")


def test_active_at_target_tier_returns_already_member(
    user: RevelUser, organization: Organization, tier_a: MembershipTier
) -> None:
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_a)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.next_step == MembershipNextStep.ALREADY_MEMBER


def test_active_at_different_tier_falls_through(
    user: RevelUser, organization: Organization, tier_a: MembershipTier, tier_b: MembershipTier
) -> None:
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_b)
    result = service.check_eligibility()
    # Falls through; no gate blocks → ALLOWED with no next_step.
    assert result.allowed is True
    assert result.next_step is None


def test_cancelled_membership_does_not_short_circuit(
    user: RevelUser, organization: Organization, tier_a: MembershipTier
) -> None:
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.CANCELLED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_a)
    result = service.check_eligibility()
    assert result.next_step is None


def test_paused_membership_at_target_tier_blocks_with_no_next_step(
    user: RevelUser, organization: Organization, tier_a: MembershipTier
) -> None:
    """PAUSED is admin/Stripe-imposed; user must not self-clear it."""
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_a)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.MEMBERSHIP_PAUSED)
    assert result.next_step is None


def test_paused_membership_at_different_tier_falls_through(
    user: RevelUser, organization: Organization, tier_a: MembershipTier, tier_b: MembershipTier
) -> None:
    """PAUSED at tier_a should not affect requests for tier_b (consistent with ACTIVE behavior)."""
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_b)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.next_step is None


def test_non_terminal_subscription_blocks_free_apply(
    user: RevelUser, organization: Organization, tier_a: MembershipTier
) -> None:
    """A user with an active subscription must not be able to free-apply (would bypass payment)."""
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier_a, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
    )
    MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    # plan=None → free path.
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_a)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.DUPLICATE_ACTIVE_SUBSCRIPTION)
    assert result.next_step is None
