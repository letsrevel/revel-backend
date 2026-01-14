"""Tests for organization admin core endpoints (details, media, contact email, Stripe)."""

from io import BytesIO

import orjson
import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from PIL import Image

from accounts.jwt import create_token
from accounts.models import RevelUser
from common.utils import assert_image_equal
from events import schema
from events.models import Organization, OrganizationStaff

pytestmark = pytest.mark.django_db


# --- Tests for PUT /organization-admin/{slug} ---


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


# --- Tests for GET /organization-admin/{slug} ---


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
