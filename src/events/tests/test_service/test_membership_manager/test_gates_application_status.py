"""Tests for ApplicationStatusGate."""

import pytest

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMembershipRequest
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import Reasons

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def test_rejected_application_blocks_with_no_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.APPLICATION_REJECTED)
    assert result.next_step is None


def test_cancelled_application_does_not_block(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.CANCELLED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True  # downstream gates allow; user may re-apply


def test_pending_application_falls_through(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    # No further gates block (no questionnaire/approval configured) → allowed.
    assert result.allowed is True
