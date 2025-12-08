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
    Event,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
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


# ---- Venue Management Tests ----


class TestVenueManagement:
    """Tests for venue CRUD endpoints."""

    def test_list_venues_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can list venues."""
        Venue.objects.create(organization=organization, name="Theater One")
        Venue.objects.create(organization=organization, name="Theater Two")

        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_list_venues_by_staff(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff can list venues."""
        Venue.objects.create(organization=organization, name="Main Hall")

        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_venues_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Test that regular members cannot list venues."""
        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = member_client.get(url)

        assert response.status_code == 403

    def test_create_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can create a venue."""
        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "New Venue", "description": "A great venue", "capacity": 500}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Venue"
        assert data["description"] == "A great venue"
        assert data["capacity"] == 500
        assert data["sectors"] == []
        assert Venue.objects.filter(organization=organization, name="New Venue").exists()

    def test_create_venue_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with edit_organization permission can create venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Staff Venue"}

        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201

    def test_create_venue_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without edit_organization permission cannot create venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Forbidden Venue"}

        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_create_venue_generates_slug(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that venue slug is auto-generated from name."""
        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Grand Ballroom"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["slug"] == "grand-ballroom"

    def test_get_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can get venue details with sectors."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        VenueSector.objects.create(venue=venue, name="Balcony")
        VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse("api:get_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Theater"
        assert len(data["sectors"]) == 2
        sector_names = {s["name"] for s in data["sectors"]}
        assert sector_names == {"Balcony", "Orchestra"}

    def test_get_venue_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that getting a non-existent venue returns 404."""
        import uuid

        url = reverse("api:get_organization_venue", kwargs={"slug": organization.slug, "venue_id": uuid.uuid4()})
        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_update_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can update a venue."""
        venue = Venue.objects.create(organization=organization, name="Old Name", capacity=100)

        url = reverse("api:update_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "New Name", "capacity": 200}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["capacity"] == 200

        venue.refresh_from_db()
        assert venue.name == "New Name"
        assert venue.capacity == 200

    def test_update_venue_preserves_slug(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that updating venue name does not change slug."""
        venue = Venue.objects.create(organization=organization, name="Original")
        original_slug = venue.slug

        url = reverse("api:update_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Changed Name"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        venue.refresh_from_db()
        assert venue.slug == original_slug

    def test_delete_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can delete a venue."""
        venue = Venue.objects.create(organization=organization, name="To Delete")
        sector = VenueSector.objects.create(venue=venue, name="Section A")
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse("api:delete_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not Venue.objects.filter(id=venue.id).exists()
        assert not VenueSector.objects.filter(id=sector.id).exists()

    def test_delete_venue_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without permission cannot delete venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        venue = Venue.objects.create(organization=organization, name="Protected")

        url = reverse("api:delete_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_staff_client.delete(url)

        assert response.status_code == 403
        assert Venue.objects.filter(id=venue.id).exists()


class TestVenueSectorManagement:
    """Tests for venue sector CRUD endpoints."""

    def test_list_sectors_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that listing sectors includes nested seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Balcony")
        VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1)
        VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2)

        url = reverse("api:list_venue_sectors", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Balcony"
        assert len(data[0]["seats"]) == 2

    def test_create_sector_without_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector without any seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "General Admission", "capacity": 500, "display_order": 1}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "General Admission"
        assert data["capacity"] == 500
        assert data["seats"] == []

    def test_create_sector_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector with nested seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Orchestra",
            "seats": [
                {"label": "A1", "row": "A", "number": 1},
                {"label": "A2", "row": "A", "number": 2},
                {"label": "B1", "row": "B", "number": 1, "is_accessible": True},
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Orchestra"
        assert len(data["seats"]) == 3

        # Verify seats were created in DB
        sector = VenueSector.objects.get(venue=venue, name="Orchestra")
        assert sector.seats.count() == 3
        assert sector.seats.filter(is_accessible=True).count() == 1

    def test_create_sector_with_shape_and_valid_seat_positions(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test creating a sector with shape and seats with valid positions inside the shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        # Square shape from (0,0) to (100,100)
        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Floor",
            "shape": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 10, "y": 10}},  # Inside
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert len(data["seats"]) == 2

    def test_create_sector_with_shape_and_invalid_seat_position(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that creating a sector with seat position outside shape fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        # Square shape from (0,0) to (100,100)
        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Floor",
            "shape": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
            "seats": [
                {"label": "A1", "position": {"x": 150, "y": 50}},  # Outside!
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Schema validation returns 422 for invalid input
        assert response.status_code == 422
        assert "outside the sector shape" in response.json()["detail"][0]["msg"]

    def test_create_sector_duplicate_name_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that creating a sector with duplicate name in same venue fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        VenueSector.objects.create(venue=venue, name="Balcony")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Balcony"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_get_sector_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test getting a single sector with its seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="VIP")
        VenueSeat.objects.create(sector=sector, label="V1")
        VenueSeat.objects.create(sector=sector, label="V2")

        url = reverse(
            "api:get_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "VIP"
        assert len(data["seats"]) == 2

    def test_update_sector_metadata(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating sector metadata without touching seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Old Name", capacity=100)
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"name": "New Name", "capacity": 200}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["capacity"] == 200
        # Seats should still exist
        assert len(data["seats"]) == 1

    def test_delete_sector(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a sector and its seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="To Delete")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:delete_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSector.objects.filter(id=sector.id).exists()
        assert not VenueSeat.objects.filter(id=seat.id).exists()


class TestVenueSeatManagement:
    """Tests for individual seat update/delete endpoints."""

    def test_update_seat_by_label(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a seat by its label."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"is_accessible": True, "is_obstructed_view": True}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["is_accessible"] is True
        assert data["is_obstructed_view"] is True

        seat.refresh_from_db()
        assert seat.is_accessible is True
        assert seat.is_obstructed_view is True

    def test_update_seat_position_within_shape(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test updating seat position when sector has shape - valid position."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"position": {"x": 50, "y": 50}}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["position"] == {"x": 50, "y": 50}

    def test_update_seat_position_outside_shape_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test updating seat position outside sector shape fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"position": {"x": 150, "y": 50}}  # Outside

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "outside the sector shape" in response.json()["detail"]

    def test_update_seat_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a non-existent seat returns 404."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "NONEXISTENT"},
        )
        payload = {"is_accessible": True}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404

    def test_bulk_create_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk creating seats in a sector."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "row": "A", "number": 1},
                {"label": "A2", "row": "A", "number": 2},
                {"label": "A3", "row": "A", "number": 3, "is_accessible": True},
            ]
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert len(data) == 3
        assert sector.seats.count() == 3
        assert sector.seats.filter(is_accessible=True).count() == 1

    def test_bulk_create_seats_with_shape_validation(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that bulk create validates seat positions against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 150, "y": 50}},  # Outside!
            ]
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "outside the sector shape" in response.json()["detail"]
        # No seats should have been created
        assert sector.seats.count() == 0

    def test_bulk_create_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that bulk create with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[dict[str, str]]] = {"seats": []}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)

    def test_delete_seat_by_label(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a seat by its label."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a non-existent seat returns 404."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "NONEXISTENT"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 404

    def test_delete_seat_blocked_by_active_future_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with active ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        # Create a future event with a ticket assigned to this seat
        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 400
        assert "active or pending tickets" in response.json()["detail"]
        # Seat should still exist
        assert VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_blocked_by_pending_future_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with pending ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.PENDING,
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 400
        assert "active or pending tickets" in response.json()["detail"]

    def test_delete_seat_allowed_with_cancelled_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with cancelled ticket can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CANCELLED,
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_allowed_with_past_event_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with ticket for past event can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        past_event = Event.objects.create(
            organization=organization,
            name="Past Concert",
            start=timezone.now() - timedelta(days=7),
            end=timezone.now() - timedelta(days=7, hours=-3),  # 3 hours after start, still in the past
            status=Event.EventStatus.CLOSED,
        )
        tier = TicketTier.objects.create(event=past_event, name="General", price=50)
        Ticket.objects.create(
            event=past_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_allowed_with_checked_in_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with checked_in ticket for future event can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        # Event starting soon (within next hour) but still future
        future_event = Event.objects.create(
            organization=organization,
            name="Ongoing Concert",
            start=timezone.now() - timedelta(hours=1),
            end=timezone.now() + timedelta(hours=2),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CHECKED_IN,
            checked_in_at=timezone.now(),
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_seat_operations_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without edit_organization permission cannot modify seats."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        # Try update
        update_url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_staff_client.put(
            update_url, data=orjson.dumps({"is_accessible": True}), content_type="application/json"
        )
        assert response.status_code == 403

        # Try delete
        delete_url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_staff_client.delete(delete_url)
        assert response.status_code == 403

    def test_bulk_delete_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk deleting seats via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")
        VenueSeat.objects.create(sector=sector, label="A3")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "A2"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["deleted"] == 2
        assert sector.seats.count() == 1
        assert sector.seats.filter(label="A3").exists()

    def test_bulk_delete_seats_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk delete with non-existent seats fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "NONEXISTENT"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404
        assert "NONEXISTENT" in response.json()["detail"]
        # A1 should still exist (atomic rollback)
        assert sector.seats.filter(label="A1").exists()

    def test_bulk_delete_seats_blocked_by_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test bulk delete blocked when seat has active ticket for future event."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "A2"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "A1" in response.json()["detail"]
        # Both seats should still exist (atomic rollback)
        assert sector.seats.count() == 2

    def test_bulk_delete_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk delete with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[str]] = {"labels": []}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)

    def test_bulk_update_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk updating seats via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A3", row="A", number=3, is_accessible=False)

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "is_accessible": True},
                {"label": "A2", "row": "B", "number": 1},
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        # Verify changes in DB
        a1 = sector.seats.get(label="A1")
        assert a1.is_accessible is True

        a2 = sector.seats.get(label="A2")
        assert a2.row == "B"
        assert a2.number == 1

        # A3 should be unchanged
        a3 = sector.seats.get(label="A3")
        assert a3.is_accessible is False
        assert a3.row == "A"

    def test_bulk_update_seats_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk update with non-existent seats fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "is_accessible": True},
                {"label": "NONEXISTENT", "is_accessible": True},
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404
        assert "NONEXISTENT" in response.json()["detail"]

        # A1 should still be unchanged (atomic rollback)
        a1 = sector.seats.get(label="A1")
        assert a1.is_accessible is False

    def test_bulk_update_seats_position_validation(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk update validates position against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 150, "y": 50}},  # Outside
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "A2" in response.json()["detail"]
        assert "outside" in response.json()["detail"]

    def test_bulk_update_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk update with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[dict[str, str]]] = {"seats": []}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)

    def test_sector_metadata_in_response(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that sector metadata is included in API responses."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        metadata = {"aisle_positions": [{"x": 50, "y": 0}], "custom_key": "value"}
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata=metadata)

        url = reverse(
            "api:get_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        assert response.json()["metadata"] == metadata

    def test_create_sector_with_metadata(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector with metadata via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        metadata = {"aisle_positions": [{"x": 50, "y": 0}], "label_offset": 10}

        url = reverse(
            "api:create_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id},
        )
        payload = {"name": "Orchestra", "metadata": metadata}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        assert response.json()["metadata"] == metadata

        sector = VenueSector.objects.get(venue=venue, name="Orchestra")
        assert sector.metadata == metadata

    def test_update_sector_metadata(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a sector's metadata via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata={"old": "data"})
        new_metadata = {"new": "data", "nested": {"key": "value"}}

        url = reverse(
            "api:update_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"metadata": new_metadata}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["metadata"] == new_metadata

        sector.refresh_from_db()
        assert sector.metadata == new_metadata


class TestPointInPolygon:
    """Tests for the point_in_polygon validation function."""

    def test_point_inside_square(self) -> None:
        """Test point inside a simple square."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=50, y=50), square) is True
        assert point_in_polygon(Coordinate2D(x=10, y=10), square) is True
        assert point_in_polygon(Coordinate2D(x=99, y=99), square) is True

    def test_point_outside_square(self) -> None:
        """Test point outside a simple square."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=150, y=50), square) is False
        assert point_in_polygon(Coordinate2D(x=-10, y=50), square) is False
        assert point_in_polygon(Coordinate2D(x=50, y=150), square) is False
        assert point_in_polygon(Coordinate2D(x=50, y=-10), square) is False

    def test_point_inside_triangle(self) -> None:
        """Test point inside a triangle."""
        from events.schema import Coordinate2D, point_in_polygon

        triangle = [Coordinate2D(x=0, y=0), Coordinate2D(x=100, y=0), Coordinate2D(x=50, y=100)]
        assert point_in_polygon(Coordinate2D(x=50, y=30), triangle) is True
        assert point_in_polygon(Coordinate2D(x=50, y=50), triangle) is True

    def test_point_outside_triangle(self) -> None:
        """Test point outside a triangle."""
        from events.schema import Coordinate2D, point_in_polygon

        triangle = [Coordinate2D(x=0, y=0), Coordinate2D(x=100, y=0), Coordinate2D(x=50, y=100)]
        assert point_in_polygon(Coordinate2D(x=10, y=90), triangle) is False
        assert point_in_polygon(Coordinate2D(x=90, y=90), triangle) is False

    def test_point_inside_complex_polygon(self) -> None:
        """Test point inside a more complex L-shaped polygon."""
        from events.schema import Coordinate2D, point_in_polygon

        # L-shape
        l_shape = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=50, y=0),
            Coordinate2D(x=50, y=50),
            Coordinate2D(x=100, y=50),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=25, y=25), l_shape) is True
        assert point_in_polygon(Coordinate2D(x=25, y=75), l_shape) is True
        assert point_in_polygon(Coordinate2D(x=75, y=75), l_shape) is True

    def test_point_outside_complex_polygon(self) -> None:
        """Test point outside a complex L-shaped polygon (in the cutout)."""
        from events.schema import Coordinate2D, point_in_polygon

        # L-shape with cutout in upper-right
        l_shape = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=50, y=0),
            Coordinate2D(x=50, y=50),
            Coordinate2D(x=100, y=50),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        # Point in the "cutout" area of the L
        assert point_in_polygon(Coordinate2D(x=75, y=25), l_shape) is False

    def test_point_on_edge_behavior(self) -> None:
        """Test behavior of points on or near edges."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        # Points very close to edges (just inside)
        assert point_in_polygon(Coordinate2D(x=1, y=50), square) is True
        assert point_in_polygon(Coordinate2D(x=99, y=50), square) is True
