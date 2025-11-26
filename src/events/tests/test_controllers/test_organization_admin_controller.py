from datetime import datetime, timedelta

import orjson
import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.jwt import create_token
from accounts.models import RevelUser
from common.utils import assert_image_equal
from events import schema
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
)

pytestmark = pytest.mark.django_db


def test_update_organization_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can successfully update their organization."""
    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    payload = {"description": "New description by owner", "visibility": "public"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["description"] == "New description by owner"
    assert data["visibility"] == "public"
    organization.refresh_from_db()
    assert organization.description == "New description by owner"


def test_upload_organization_logo_by_owner(
    organization_owner_client: Client, organization: Organization, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an organization owner can successfully upload their logo."""
    url = reverse("api:org_upload_logo", kwargs={"slug": organization.slug})

    response = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")

    assert response.status_code == 200
    organization.refresh_from_db()
    organization.logo.seek(0)
    assert_image_equal(organization.logo.read(), png_bytes)


def test_upload_organization_cover_art_by_owner(
    organization_owner_client: Client, organization: Organization, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an organization owner can successfully update their cover art."""
    url = reverse("api:org_upload_cover_art", kwargs={"slug": organization.slug})

    response = organization_owner_client.post(url, data={"cover_art": png_file}, format="multipart")

    assert response.status_code == 200
    organization.refresh_from_db()
    organization.cover_art.seek(0)
    assert_image_equal(organization.cover_art.read(), png_bytes)


def test_delete_organization_logo_by_owner(
    organization_owner_client: Client, organization: Organization, png_file: SimpleUploadedFile
) -> None:
    """Test that an organization owner can successfully delete their logo."""
    # First upload a logo
    upload_url = reverse("api:org_upload_logo", kwargs={"slug": organization.slug})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    organization.refresh_from_db()
    assert organization.logo

    # Now delete it
    delete_url = reverse("api:org_delete_logo", kwargs={"slug": organization.slug})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    organization.refresh_from_db()
    assert not organization.logo


def test_delete_organization_cover_art_by_owner(
    organization_owner_client: Client, organization: Organization, png_file: SimpleUploadedFile
) -> None:
    """Test that an organization owner can successfully delete their cover art."""
    # First upload cover art
    upload_url = reverse("api:org_upload_cover_art", kwargs={"slug": organization.slug})
    organization_owner_client.post(upload_url, data={"cover_art": png_file}, format="multipart")
    organization.refresh_from_db()
    assert organization.cover_art

    # Now delete it
    delete_url = reverse("api:org_delete_cover_art", kwargs={"slug": organization.slug})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    organization.refresh_from_db()
    assert not organization.cover_art


def test_delete_organization_logo_when_none_exists(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that deleting a logo when none exists is idempotent (returns 204)."""
    assert not organization.logo

    url = reverse("api:org_delete_logo", kwargs={"slug": organization.slug})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_organization_logo_by_staff_with_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff with edit_organization permission can delete logo."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["edit_organization"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo first
    upload_url = reverse("api:org_upload_logo", kwargs={"slug": organization.slug})
    organization_staff_client.post(upload_url, data={"logo": png_file}, format="multipart")
    organization.refresh_from_db()
    assert organization.logo

    # Delete it
    delete_url = reverse("api:org_delete_logo", kwargs={"slug": organization.slug})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 204
    organization.refresh_from_db()
    assert not organization.logo


def test_delete_organization_logo_by_staff_without_permission(
    organization_staff_client: Client,
    organization_owner_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff without edit_organization permission cannot delete logo."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["edit_organization"] = False
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo as owner first
    upload_url = reverse("api:org_upload_logo", kwargs={"slug": organization.slug})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    organization.refresh_from_db()
    assert organization.logo

    # Try to delete as staff without permission
    delete_url = reverse("api:org_delete_logo", kwargs={"slug": organization.slug})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 403
    organization.refresh_from_db()
    assert organization.logo


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 403), ("client", 401)],
)
def test_delete_organization_logo_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that users without owner/staff roles get appropriate error when trying to delete logo."""
    organization.visibility = "public"
    organization.save()
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:org_delete_logo", kwargs={"slug": organization.slug})

    response = client.delete(url)
    assert response.status_code == expected_status_code


def test_upload_organization_logo_replaces_old_file(
    organization_owner_client: Client, organization: Organization, png_file: SimpleUploadedFile
) -> None:
    """Test that uploading a new logo deletes the old one."""
    url = reverse("api:org_upload_logo", kwargs={"slug": organization.slug})

    # Upload first logo
    response1 = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")
    assert response1.status_code == 200
    organization.refresh_from_db()
    old_logo_name = organization.logo.name

    # Upload second logo
    from io import BytesIO

    from PIL import Image

    # Create a different image (blue square)
    img = Image.new("RGB", (150, 150), color="blue")
    img_bytes = BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    new_png_file = SimpleUploadedFile("new_logo.png", img_bytes.read(), content_type="image/png")

    response2 = organization_owner_client.post(url, data={"logo": new_png_file}, format="multipart")
    assert response2.status_code == 200
    organization.refresh_from_db()
    new_logo_name = organization.logo.name

    # Verify that the logo name changed (different file)
    assert old_logo_name != new_logo_name

    # Verify the new file is saved correctly
    assert organization.logo
    organization.logo.seek(0)
    saved_image = Image.open(organization.logo)
    assert saved_image.size == (150, 150)


def test_update_organization_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'edit_organization' permission can update."""
    perms = staff_member.permissions
    perms["default"]["edit_organization"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    payload = {"description": "Updated by staff", "visibility": "members-only"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    organization.refresh_from_db()
    assert organization.description == "Updated by staff"


def test_update_organization_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'edit_organization' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["edit_organization"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    payload = {"description": "This update should fail", "visibility": "public"}
    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 403), ("client", 401)],
)
def test_update_organization_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that users without owner/staff roles get a 403 when trying to update."""
    organization.visibility = "public"
    organization.save()
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    payload = {"description": "This should fail", "visibility": "public"}

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_create_event_series_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create an event series."""
    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "New Event Series", "description": "A series of events."}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Event Series"
    assert data["organization"]["slug"] == organization.slug


def test_create_event_series_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event_series' permission can create an event series."""
    perms = staff_member.permissions
    perms["default"]["create_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "Staff-Created Series", "description": "Created by staff."}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Staff-Created Series"


def test_create_event_series_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event_series' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["create_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "This should not be created", "description": "This should fail."}
    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 404), ("client", 401)],
)
def test_create_event_series_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that unauthorized users get a 403 or 401 when trying to create an event series."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "Unauthorized Series", "description": "This should fail."}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_create_event_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create an event."""
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Event",
        "description": "A single event.",
        "visibility": "public",
        "status": "open",
        "event_type": "public",
        "waitlist_open": False,
        # "requires_ticket": False,  we test default behavior
        "address": "123 Main St",
        "start": timezone.now().timestamp(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Event"
    assert data["organization"]["slug"] == organization.slug
    assert data["requires_ticket"] is False


def test_create_event_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event' permission can create an event."""
    perms = staff_member.permissions
    perms["default"]["create_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "Staff-Created Event",
        "description": "Created by staff.",
        "visibility": "public",
        "status": "open",
        "event_type": "public",
        "waitlist_open": False,
        "requires_ticket": False,
        "address": "123 Main St",
        "start": timezone.now().timestamp(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Staff-Created Event"


def test_create_event_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["create_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "This should not be created",
        "description": "This should fail.",
        "start": timezone.now().timestamp(),
    }
    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 404), ("client", 401)],
)
def test_create_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that unauthorized users get a 403 or 401 when trying to create an event."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {"name": "Unauthorized Event", "description": "This should fail.", "start": timezone.now().timestamp()}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_list_organization_tokens(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can list tokens."""
    url = reverse("api:list_organization_tokens", kwargs={"slug": organization.slug})
    response = organization_owner_client.get(url)
    assert response.status_code == 200


def test_create_organization_token(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create a token."""
    # Get the default tier
    from events.models import MembershipTier

    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")

    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Token",
        "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
        "membership_tier_id": str(default_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Token"


def test_update_organization_token(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Test that an organization owner can update a token."""
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {"name": "Updated Token Name"}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Token Name"


def test_delete_organization_token(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Test that an organization owner can delete a token."""
    url = reverse(
        "api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id}
    )
    response = organization_owner_client.delete(url)
    assert response.status_code == 204
    assert not OrganizationToken.objects.filter(id=organization_token.id).exists()


class TestManageMembershipRequests:
    def test_list_membership_requests_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that an organization owner can list membership requests."""
        url = reverse("api:list_membership_requests", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200

    def test_approve_membership_request_by_owner(
        self, organization_owner_client: Client, organization_membership_request: OrganizationMembershipRequest
    ) -> None:
        """Test that an organization owner can approve a membership request."""
        # Get the default tier
        tier = MembershipTier.objects.get(
            organization=organization_membership_request.organization, name="General membership"
        )

        url = reverse(
            "api:approve_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        payload = {"tier_id": str(tier.id)}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 204
        organization_membership_request.refresh_from_db()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.APPROVED

        # Verify member was created with correct tier
        member = OrganizationMember.objects.get(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        )
        assert member.tier == tier
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_reject_membership_request_by_owner(
        self, organization_owner_client: Client, organization_membership_request: OrganizationMembershipRequest
    ) -> None:
        """Test that an organization owner can reject a membership request."""
        url = reverse(
            "api:reject_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        response = organization_owner_client.post(url)
        assert response.status_code == 204
        organization_membership_request.refresh_from_db()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.REJECTED


class TestManageMembersAndStaff:
    def test_list_members(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Test listing organization members."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == member_user.email

    def test_list_staff(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test listing organization staff."""
        url = reverse("api:list_organization_staff", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == staff_member.user.email

    def test_remove_member(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Test removing a member from an organization."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        url = reverse("api:remove_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()

    def test_add_staff(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test adding a new staff member."""
        url = reverse("api:create_organization_staff", kwargs={"slug": organization.slug, "user_id": nonmember_user.id})
        response = organization_owner_client.post(url, content_type="application/json")
        assert response.status_code == 201, response.content
        assert OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_remove_staff(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test removing a staff member."""
        url = reverse(
            "api:remove_organization_staff", kwargs={"slug": organization.slug, "user_id": staff_member.user.id}
        )
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not OrganizationStaff.objects.filter(organization=organization, user=staff_member.user).exists()

    def test_update_staff_permissions_by_owner(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that the owner can update staff permissions."""
        url = reverse(
            "api:update_staff_permissions", kwargs={"slug": organization.slug, "user_id": staff_member.user.id}
        )
        payload = {"default": {"manage_members": True, "create_event": True}, "event_overrides": {}}
        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        staff_member.refresh_from_db()
        assert staff_member.permissions["default"]["manage_members"] is True
        assert staff_member.permissions["default"]["create_event"] is True

    def test_update_staff_permissions_by_staff_fails(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that a staff member cannot update another staff member's permissions."""
        # Create another staff member for the test
        other_staff_user = RevelUser.objects.create_user("otherstaff@example.com")
        OrganizationStaff.objects.create(organization=organization, user=other_staff_user)

        url = reverse(
            "api:update_staff_permissions", kwargs={"slug": organization.slug, "user_id": other_staff_user.id}
        )
        payload = {"default": {"manage_members": True}}
        response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403


class TestGetOrganizationAdmin:
    """Tests for the GET organization admin endpoint."""

    def test_get_organization_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that an organization owner can get comprehensive organization details."""
        # Set some platform fee and stripe fields to test they are returned
        organization.platform_fee_fixed = 2.50
        organization.stripe_account_id = "acct_test123"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save()

        url = reverse("api:get_organization_admin", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()

        # Verify basic fields
        assert data["id"] == str(organization.id)
        assert data["name"] == organization.name
        assert data["slug"] == organization.slug
        assert data["visibility"] == organization.visibility

        # Verify platform fee fields
        assert data["platform_fee_percent"] is not None
        assert data["platform_fee_fixed"] == "2.50"

        # Verify Stripe fields
        assert data["stripe_account_id"] == "acct_test123"
        assert data["stripe_charges_enabled"] is True
        assert data["stripe_details_submitted"] is True
        assert data["is_stripe_connected"] is True

    def test_get_organization_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with view_organization permission can get organization details."""
        # Grant permission
        perms = staff_member.permissions
        perms["default"]["view_organization"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:get_organization_admin", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["slug"] == organization.slug

    @pytest.mark.parametrize(
        "client_fixture,expected_status_code",
        [("member_client", 403), ("nonmember_client", 404), ("client", 401)],
    )
    def test_get_organization_by_unauthorized_users(
        self, request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
    ) -> None:
        """Test that users without owner/staff roles get appropriate error when trying to get organization details."""
        client: Client = request.getfixturevalue(client_fixture)
        url = reverse("api:get_organization_admin", kwargs={"slug": organization.slug})

        response = client.get(url)
        assert response.status_code == expected_status_code

    def test_get_organization_without_stripe_connection(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that organization details are correctly returned when Stripe is not connected."""
        # Ensure Stripe is not connected
        organization.stripe_account_id = None
        organization.stripe_charges_enabled = False
        organization.stripe_details_submitted = False
        organization.save()

        url = reverse("api:get_organization_admin", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["stripe_account_id"] is None
        assert data["stripe_charges_enabled"] is False
        assert data["stripe_details_submitted"] is False
        assert data["is_stripe_connected"] is False


# ---- Membership Tier Tests ----


def test_list_membership_tiers_by_staff(organization_staff_client: Client, organization: Organization) -> None:
    """Test that staff can list membership tiers."""
    # Create some tiers (note: organization signal already creates "General membership")
    MembershipTier.objects.create(organization=organization, name="Gold")
    MembershipTier.objects.create(organization=organization, name="Silver")

    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3  # General membership + Gold + Silver
    tier_names = {tier["name"] for tier in data}
    assert tier_names == {"General membership", "Gold", "Silver"}


def test_list_membership_tiers_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can list membership tiers."""
    MembershipTier.objects.create(organization=organization, name="Premium")

    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2  # General membership + Premium
    tier_names = {tier["name"] for tier in data}
    assert "Premium" in tier_names
    assert "General membership" in tier_names


def test_list_membership_tiers_by_member_forbidden(member_client: Client, organization: Organization) -> None:
    """Test that regular members cannot list membership tiers."""
    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = member_client.get(url)

    assert response.status_code == 403


def test_create_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can create a membership tier."""
    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "VIP"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "VIP"
    assert "id" in data

    # Verify it was created in DB
    assert MembershipTier.objects.filter(organization=organization, name="VIP").exists()


def test_create_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can create tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Bronze"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Bronze"


def test_create_membership_tier_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff without manage_members permission cannot create tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Platinum"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_create_duplicate_membership_tier_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that creating a tier with duplicate name in same org fails."""
    MembershipTier.objects.create(organization=organization, name="Gold")

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Gold"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_create_membership_tier_with_empty_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that creating a tier with empty/whitespace-only name fails."""
    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "   "}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422


def test_update_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can update a membership tier."""
    tier = MembershipTier.objects.create(organization=organization, name="Old Name")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    payload = {"name": "New Name"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name"

    tier.refresh_from_db()
    assert tier.name == "New Name"


def test_update_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can update tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Original")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    payload = {"name": "Updated"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    tier.refresh_from_db()
    assert tier.name == "Updated"


def test_update_membership_tier_to_duplicate_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that updating a tier to a duplicate name fails."""
    MembershipTier.objects.create(organization=organization, name="Gold")
    tier2 = MembershipTier.objects.create(organization=organization, name="Silver")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier2.id})
    payload = {"name": "Gold"}  # Duplicate name

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_update_membership_tier_of_another_organization_fails(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that updating a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": other_tier.id})
    payload = {"name": "Hacked Name"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_delete_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can delete a membership tier."""
    tier = MembershipTier.objects.create(organization=organization, name="To Delete")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_membership_tier_sets_members_tier_to_null(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that deleting a tier sets members' tier FK to NULL."""
    tier = MembershipTier.objects.create(organization=organization, name="To Delete")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    member.refresh_from_db()
    assert member.tier is None


def test_delete_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can delete tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Deletable")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_membership_tier_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff without manage_members permission cannot delete tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Protected")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403


def test_delete_membership_tier_of_another_organization_fails(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that deleting a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": other_tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
    # Ensure it wasn't deleted
    assert MembershipTier.objects.filter(id=other_tier.id).exists()


# ---- Organization Member Update Tests ----


def test_update_member_status_by_owner(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update member status."""
    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "paused"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "paused"

    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.PAUSED


def test_update_member_tier_by_owner(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update member tier."""
    tier = MembershipTier.objects.create(organization=organization, name="Premium")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=None)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": str(tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["tier"]["id"] == str(tier.id)
    assert data["tier"]["name"] == "Premium"

    member.refresh_from_db()
    assert member.tier == tier


def test_update_member_both_status_and_tier(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update both status and tier simultaneously."""
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE, tier=None
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "cancelled", "tier_id": str(tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"
    assert data["tier"]["name"] == "Gold"

    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.CANCELLED
    assert member.tier == tier


def test_update_member_remove_tier(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can remove tier assignment by setting tier_id to null."""
    tier = MembershipTier.objects.create(organization=organization, name="Basic")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": None}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["tier"] is None

    member.refresh_from_db()
    assert member.tier is None


def test_update_member_by_staff_with_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member_user: RevelUser,
) -> None:
    """Test that staff with manage_members permission can update members."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "banned"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.BANNED


def test_update_member_by_staff_without_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member_user: RevelUser,
) -> None:
    """Test that staff without manage_members permission cannot update members."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "paused"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_update_member_with_tier_from_another_org_fails(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Test that assigning a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    member = OrganizationMember.objects.create(organization=organization, user=member_user)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": str(other_tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404
    member.refresh_from_db()
    assert member.tier is None


def test_update_member_nonexistent_member_fails(
    organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
) -> None:
    """Test that updating a non-member fails with 404."""
    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id})
    payload = {"status": "paused"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_update_member_invalid_status_fails(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that updating with invalid status value fails."""
    OrganizationMember.objects.create(organization=organization, user=member_user)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "invalid_status"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422


# --- Tests for POST /organization-admin/{slug}/update-contact-email ---


@pytest.mark.django_db(transaction=True)
class TestUpdateContactEmail:
    """Tests for the update-contact-email endpoint."""

    def test_update_contact_email_success(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test updating contact email successfully."""
        url = reverse("api:update_contact_email", kwargs={"slug": organization.slug})
        payload = {"email": "newemail@example.com"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["contact_email"] == "newemail@example.com"
        assert data["contact_email_verified"] is False

    def test_update_contact_email_auto_verifies_with_owner_email(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that contact email is auto-verified when it matches owner's verified email."""
        organization_owner_user.email_verified = True
        organization_owner_user.email = "owner@example.com"
        organization_owner_user.save()

        url = reverse("api:update_contact_email", kwargs={"slug": organization.slug})
        payload = {"email": "owner@example.com"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["contact_email"] == "owner@example.com"
        assert data["contact_email_verified"] is True

    def test_update_contact_email_same_email_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that updating to the same email fails."""
        organization.contact_email = "existing@example.com"
        organization.save()

        url = reverse("api:update_contact_email", kwargs={"slug": organization.slug})
        payload = {"email": "existing@example.com"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "already the contact email" in response.json()["detail"]

    def test_update_contact_email_requires_permission(self, member_client: Client, organization: Organization) -> None:
        """Test that updating contact email requires edit_organization permission."""
        url = reverse("api:update_contact_email", kwargs={"slug": organization.slug})
        payload = {"email": "newemail@example.com"}

        response = member_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_update_contact_email_unauthenticated_fails(self, client: Client, organization: Organization) -> None:
        """Test that unauthenticated users cannot update contact email."""
        url = reverse("api:update_contact_email", kwargs={"slug": organization.slug})
        payload = {"email": "newemail@example.com"}

        response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 401


# --- Tests for POST /organization-admin/{slug}/verify-contact-email ---


@pytest.mark.django_db(transaction=True)
class TestVerifyContactEmail:
    """Tests for the verify-contact-email endpoint."""

    def test_verify_contact_email_success(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test verifying contact email with valid token."""

        organization.contact_email = "test@example.com"
        organization.contact_email_verified = False
        organization.save()

        # Create a valid token
        from django.utils import timezone

        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=organization.id,
            user_id=organization_owner_user.id,
            email="test@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        url = reverse("api:verify_contact_email", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(url, data={"token": token}, content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["contact_email_verified"] is True

    def test_verify_contact_email_invalid_token_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that invalid token returns 400."""
        url = reverse("api:verify_contact_email", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(url, data={"token": "invalid-token"}, content_type="application/json")

        assert response.status_code == 400, response.content

    def test_verify_contact_email_wrong_email_fails(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that verification fails when email has changed."""

        organization.contact_email = "current@example.com"
        organization.contact_email_verified = False
        organization.save()

        # Create a token for a different email
        from django.utils import timezone

        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=organization.id,
            user_id=organization_owner_user.id,
            email="old@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        url = reverse("api:verify_contact_email", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(url, data={"token": token}, content_type="application/json")

        assert response.status_code == 400, response.content
        assert "different email address" in response.json()["detail"]
