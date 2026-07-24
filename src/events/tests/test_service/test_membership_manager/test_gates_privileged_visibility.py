"""Tests for PrivilegedAccessGate and OrgVisibilityGate."""

import pytest

from accounts.models import RevelUser
from events.models import Organization, OrganizationStaff
from events.service.membership_manager import MembershipEligibilityService

pytestmark = pytest.mark.django_db


def test_owner_short_circuits_allowed(organization_owner_user: RevelUser, organization: Organization) -> None:
    service = MembershipEligibilityService(user=organization_owner_user, organization=organization)
    result = service.check_eligibility()
    assert result.allowed is True


def test_staff_short_circuits_allowed(
    organization_staff_user: RevelUser,
    staff_member: OrganizationStaff,
    organization: Organization,
) -> None:
    service = MembershipEligibilityService(user=organization_staff_user, organization=organization)
    result = service.check_eligibility()
    assert result.allowed is True


def test_anonymous_visible_public_org_falls_through(user: RevelUser, organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    service = MembershipEligibilityService(user=user, organization=organization)
    result = service.check_eligibility()
    # No gates remaining → ALLOWED with no next_step yet.
    assert result.allowed is True


def test_private_org_invisible_to_non_member_blocks(user: RevelUser, organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save(update_fields=["visibility"])
    service = MembershipEligibilityService(user=user, organization=organization)
    result = service.check_eligibility()
    from events.service.membership_manager.enums import Reasons

    assert result.allowed is False
    assert result.reason == str(Reasons.ORG_NOT_VISIBLE)
    assert result.next_step is None
