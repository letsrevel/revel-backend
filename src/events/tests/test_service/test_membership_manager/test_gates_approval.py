"""Tests for ManualApprovalGate."""

import pytest

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMembershipRequest
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, Reasons

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def test_requires_approval_with_no_application_blocks_with_wait(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.REQUIRES_APPROVAL)
    assert result.next_step == MembershipNextStep.WAIT_FOR_APPROVAL


def test_approved_application_passes(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True


def test_tier_override_skips_approval(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    tier.requires_membership_approval = False
    tier.save(update_fields=["requires_membership_approval"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
