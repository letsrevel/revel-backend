"""Sanity check that the service skeleton imports and runs the (empty) gate chain."""

import pytest

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMembershipRequest
from events.service.membership_manager import MembershipEligibilityService

pytestmark = pytest.mark.django_db


def test_empty_gate_chain_returns_allowed(user: RevelUser, organization: Organization) -> None:
    # Ensure org is visible to the non-privileged user so OrgVisibilityGate falls through.
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])
    service = MembershipEligibilityService(user=user, organization=organization)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.organization_id == organization.pk


def test_applications_by_tier_keeps_newest_per_tier(user: RevelUser, organization: Organization) -> None:
    """When multiple historical applications exist for the same tier, prefetch keeps the newest."""
    tier = MembershipTier.objects.create(organization=organization, name="Standard")
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        # Terminal so the partial unique constraint allows another row.
        status=OrganizationMembershipRequest.Status.REJECTED,
    )
    new_app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    assert service.applications_by_tier[tier.pk].pk == new_app.pk


def test_advance_application_tier_less_plan_less_stays_pending(user: RevelUser, organization: Organization) -> None:
    """Tier-less + plan-less applications stay PENDING; staff must approve to assign a tier."""
    from events.service.membership_manager import advance_application

    organization.accept_membership_requests = True
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["accept_membership_requests", "visibility"])
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    advanced, _eligibility = advance_application(app)
    assert advanced.status == OrganizationMembershipRequest.Status.PENDING
