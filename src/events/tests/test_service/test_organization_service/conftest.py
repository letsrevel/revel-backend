"""Shared fixtures for the organization_service tests."""

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationToken,
)


@pytest.fixture
def organization_token(organization: Organization, organization_owner_user: RevelUser) -> OrganizationToken:
    """An organization token that grants membership."""

    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return OrganizationToken.objects.create(
        organization=organization, issuer=organization_owner_user, membership_tier=default_tier
    )


@pytest.fixture
def staff_organization_token(organization: Organization, organization_owner_user: RevelUser) -> OrganizationToken:
    """An organization token that grants staff permissions."""
    return OrganizationToken.objects.create(
        organization=organization, issuer=organization_owner_user, grants_staff_status=True, grants_membership=False
    )
