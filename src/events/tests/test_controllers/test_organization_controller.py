import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Organization, OrganizationMember, OrganizationMembershipRequest, OrganizationToken

pytestmark = pytest.mark.django_db


# --- Tests for GET /organizations/ ---


def test_list_organizations_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    organization_staff_client: Client,
    organization_owner_client: Client,
    superuser_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Test that the organization list respects user visibility rules."""
    # `organization` is private by default and is linked to all relevant clients.
    public_org = Organization.objects.create(
        name="Public Org", slug="public-org", owner=organization_owner_user, visibility=Organization.Visibility.PUBLIC
    )
    url = reverse("api:list_organizations")

    # Anonymous and non-member clients should only see the public organization.
    for c in [client, nonmember_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["name"] == public_org.name

    # Member, staff, and owner should see the public org and their own private org.
    for c in [member_client, organization_staff_client, organization_owner_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        names = {org["name"] for org in data["results"]}
        assert {organization.name, public_org.name} == names

    # Superuser sees all organizations.
    response = superuser_client.get(url)
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_list_organizations_search(client: Client, organization_owner_user: RevelUser) -> None:
    """Test searching for organizations by name and description."""
    Organization.objects.create(
        name="Tech Conference",
        slug="tech",
        owner=organization_owner_user,
        visibility="public",
        description="A conference about technology.",
    )
    Organization.objects.create(
        name="Art Fair",
        slug="art",
        owner=organization_owner_user,
        visibility="public",
        description="A fair for artists.",
    )
    url = reverse("api:list_organizations")

    # Search by name
    response = client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Tech Conference"

    # Search by description
    response = client.get(url, {"search": "artists"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Art Fair"

    # No results
    response = client.get(url, {"search": "nonexistent"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 0


# --- Tests for GET /organizations/{slug}/ ---


def test_get_organization_visibility(
    client: Client, nonmember_client: Client, member_client: Client, organization: Organization
) -> None:
    """Test retrieving a single organization based on visibility rules."""
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Initially private, anonymous/non-member can't see it, but member can.
    organization.visibility = "private"
    organization.save()
    assert client.get(url).status_code == 404
    assert nonmember_client.get(url).status_code == 404
    assert member_client.get(url).status_code == 200

    # When public, everyone can see it.
    organization.visibility = "public"
    organization.save()
    assert client.get(url).status_code == 200
    assert nonmember_client.get(url).status_code == 200
    assert member_client.get(url).status_code == 200


def test_get_organization_by_privileged_users(
    organization_owner_client: Client, organization_staff_client: Client, organization: Organization
) -> None:
    """Test that owner and staff can retrieve a private organization."""
    organization.visibility = "private"
    organization.save()
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Owner can see it.
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    assert response.json()["name"] == organization.name

    # Staff can see it.
    response = organization_staff_client.get(url)
    assert response.status_code == 200
    assert response.json()["name"] == organization.name


def test_get_organization_not_found(client: Client) -> None:
    """Test that a 404 is returned for a non-existent organization slug."""
    url = reverse("api:get_organization", kwargs={"slug": "non-existent-slug"})
    response = client.get(url)
    assert response.status_code == 404


class TestClaimInvitation:
    def test_claim_invitation_success(
        self, nonmember_client: Client, organization_token: OrganizationToken, nonmember_user: RevelUser
    ) -> None:
        """Test that an invitation is claimed successfully."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": organization_token.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        assert OrganizationMember.objects.filter(
            organization=organization_token.organization, user=nonmember_user
        ).exists()

    def test_claim_invitation_unauthorized(self, client: Client, organization_token: OrganizationToken) -> None:
        """Test that an unauthenticated user cannot claim an invitation."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": organization_token.id})
        response = client.post(url)
        assert response.status_code == 401

    def test_claim_invitation_invalid_token(self, nonmember_client: Client) -> None:
        """Test that an invalid token returns a 400."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": "invalid-token"})
        response = nonmember_client.post(url)
        assert response.status_code == 400


class TestCreateMembershipRequest:
    def test_create_membership_request_success(
        self, nonmember_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test that a membership request is created successfully."""
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = nonmember_client.post(url, content_type="application/json")
        assert response.status_code == 200, response.json()
        assert OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_create_membership_request_unauthorized(self, client: Client, organization: Organization) -> None:
        """Test that an unauthenticated user cannot create a membership request."""
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = client.post(url)
        assert response.status_code == 401

    def test_create_membership_request_already_member(self, member_client: Client, organization: Organization) -> None:
        """Test that a member cannot create a membership request."""
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = member_client.post(url)
        assert response.status_code == 400
