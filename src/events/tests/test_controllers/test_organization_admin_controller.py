from datetime import datetime, timedelta

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from common.utils import assert_image_equal
from events.models import (
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
        "free_for_members": False,
        "free_for_staff": False,
        "requires_ticket": False,
        "address": "123 Main St",
        "start": timezone.now().timestamp(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Event"
    assert data["organization"]["slug"] == organization.slug


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
        "free_for_members": False,
        "free_for_staff": False,
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
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {"name": "New Token", "expires_at": (datetime.now() + timedelta(days=30)).isoformat()}
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
        url = reverse(
            "api:approve_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        response = organization_owner_client.post(url)
        assert response.status_code == 204
        organization_membership_request.refresh_from_db()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.APPROVED
        assert OrganizationMember.objects.filter(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        ).exists()

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
