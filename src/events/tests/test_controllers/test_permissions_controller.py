import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMember

pytestmark = pytest.mark.django_db


def test_my_permissions_includes_membership_tiers(
    member_client: Client,
    organization: Organization,
    member_user: RevelUser,
) -> None:
    """Test that my_permissions endpoint returns membership tier information."""
    # Get the default "General membership" tier that was created by the signal
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")

    # Update the existing membership (created by member_client fixture) to have a tier
    member = OrganizationMember.objects.get(organization=organization, user=member_user)
    member.tier = default_tier
    member.save()

    # Create a second organization with a Gold tier
    org2 = Organization.objects.create(name="Org2", slug="org2", owner=member_user)
    gold_tier = MembershipTier.objects.create(organization=org2, name="Gold")
    OrganizationMember.objects.create(organization=org2, user=member_user, tier=gold_tier)

    # Create a third organization with no tier assigned
    org3 = Organization.objects.create(name="Org3", slug="org3", owner=member_user)
    OrganizationMember.objects.create(organization=org3, user=member_user, tier=None)

    url = reverse("api:my_permissions")
    response = member_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Check that memberships is a dict
    assert isinstance(data["memberships"], dict)

    # Check that the first organization has the General membership tier
    org_id_str = str(organization.id)
    assert org_id_str in data["memberships"]
    assert isinstance(data["memberships"][org_id_str], dict)
    assert data["memberships"][org_id_str]["id"] == str(default_tier.id)
    assert data["memberships"][org_id_str]["name"] == "General membership"

    # Check that the second organization has the Gold tier
    org2_id_str = str(org2.id)
    assert org2_id_str in data["memberships"]
    assert isinstance(data["memberships"][org2_id_str], dict)
    assert data["memberships"][org2_id_str]["id"] == str(gold_tier.id)
    assert data["memberships"][org2_id_str]["name"] == "Gold"

    # Check that the third organization has literal "member" (no tier)
    org3_id_str = str(org3.id)
    assert org3_id_str in data["memberships"]
    assert data["memberships"][org3_id_str] == "member"

    # Check organization_permissions (org2 and org3 are owned by member_user)
    assert data["organization_permissions"] is not None
    assert org2_id_str in data["organization_permissions"]
    assert data["organization_permissions"][org2_id_str] == "owner"
    assert org3_id_str in data["organization_permissions"]
    assert data["organization_permissions"][org3_id_str] == "owner"
