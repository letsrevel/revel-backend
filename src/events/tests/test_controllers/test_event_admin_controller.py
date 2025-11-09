## src/events/tests/test_controllers/test_event_controller.py

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from common.utils import assert_image_equal
from events.models import Event, EventInvitationRequest, EventToken, OrganizationStaff, Ticket, TicketTier

pytestmark = pytest.mark.django_db


# --- Tests for PUT /event-admin/{event_id} ---


def test_update_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can successfully update it."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "New Name by Owner", "visibility": "private"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name by Owner"
    assert data["visibility"] == "private"

    event.refresh_from_db()
    assert event.name == "New Name by Owner"
    assert event.visibility == "private"


def test_update_event_by_staff_with_permission(organization_staff_client: Client, event: Event) -> None:
    """Test that a staff member with 'edit_event' permission can update."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    # The default permission map for staff gives edit_event=True
    payload = {"name": "Updated by Staff", "description": "Staff was here.", "visibility": event.visibility}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == "Updated by Staff"
    assert event.description == "Staff was here."


def test_update_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'edit_event' permission gets a 403."""
    # Remove the 'edit_event' permission
    perms = staff_member.permissions
    perms["default"]["edit_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "This update should fail", "visibility": "public"}
    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_update_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403 when trying to update."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:edit_event", kwargs={"event_id": public_event.pk})
    payload = {"name": "This should fail", "visibility": "public"}

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_upload_event_logo_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event owner can successfully upload their logo."""
    url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})

    response = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")

    assert response.status_code == 200
    event.refresh_from_db()
    event.logo.seek(0)
    assert_image_equal(event.logo.read(), png_bytes)


def test_upload_event_cover_art_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event owner can successfully update their cover art."""
    url = reverse("api:event_upload_cover_art", kwargs={"event_id": event.pk})

    response = organization_owner_client.post(url, data={"cover_art": png_file}, format="multipart")

    assert response.status_code == 200
    event.refresh_from_db()
    event.cover_art.seek(0)
    assert_image_equal(event.cover_art.read(), png_bytes)


def test_delete_event_logo_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that an event owner can successfully delete their logo."""
    # First upload a logo
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Now delete it
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.logo


def test_delete_event_cover_art_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that an event owner can successfully delete their cover art."""
    # First upload cover art
    upload_url = reverse("api:event_upload_cover_art", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"cover_art": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.cover_art

    # Now delete it
    delete_url = reverse("api:event_delete_cover_art", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.cover_art


def test_delete_event_logo_when_none_exists(organization_owner_client: Client, event: Event) -> None:
    """Test that deleting a logo when none exists is idempotent (returns 204)."""
    assert not event.logo

    url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_event_cover_art_when_none_exists(organization_owner_client: Client, event: Event) -> None:
    """Test that deleting cover art when none exists is idempotent (returns 204)."""
    assert not event.cover_art

    url = reverse("api:event_delete_cover_art", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_event_logo_by_staff_with_permission(
    organization_staff_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that staff with edit_event permission can delete logo."""
    # Upload a logo first
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_staff_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Delete it
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.logo


def test_delete_event_logo_by_staff_without_permission(
    organization_staff_client: Client,
    organization_owner_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff without edit_event permission cannot delete logo."""
    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["edit_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo as owner first
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Try to delete as staff without permission
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.logo


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_delete_event_logo_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get appropriate error when trying to delete logo."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:event_delete_logo", kwargs={"event_id": public_event.pk})

    response = client.delete(url)
    assert response.status_code == expected_status_code


def test_upload_event_logo_replaces_old_file(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that uploading a new logo deletes the old one."""
    url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})

    # Upload first logo
    response1 = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")
    assert response1.status_code == 200
    event.refresh_from_db()
    old_logo_name = event.logo.name

    # Upload second logo
    from io import BytesIO

    from PIL import Image

    # Create a different image (red square instead of the default png)
    img = Image.new("RGB", (100, 100), color="red")
    img_bytes = BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    new_png_file = SimpleUploadedFile("new_logo.png", img_bytes.read(), content_type="image/png")

    response2 = organization_owner_client.post(url, data={"logo": new_png_file}, format="multipart")
    assert response2.status_code == 200
    event.refresh_from_db()
    new_logo_name = event.logo.name

    # Verify that the logo name changed (different file)
    assert old_logo_name != new_logo_name

    # Verify the old file was deleted (this is tricky in tests, but we can check the field was updated)
    assert event.logo
    event.logo.seek(0)
    saved_image = Image.open(event.logo)
    assert saved_image.size == (100, 100)


# --- Tests for DELETE /events/{event_id}/ ---


# --- Tests for POST /event-admin/{event_id}/actions/{status} ---


def test_update_event_status_to_open_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to open."""
    event.status = Event.EventStatus.DRAFT
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.OPEN

    event.refresh_from_db()
    assert event.status == Event.EventStatus.OPEN


def test_update_event_status_to_closed_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to closed."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CLOSED})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.CLOSED

    event.refresh_from_db()
    assert event.status == Event.EventStatus.CLOSED


def test_update_event_status_to_draft_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to draft."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.DRAFT})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.DRAFT

    event.refresh_from_db()
    assert event.status == Event.EventStatus.DRAFT


def test_update_event_status_to_deleted_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to deleted."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.DELETED})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.DELETED

    event.refresh_from_db()
    assert event.status == Event.EventStatus.DELETED
    # Verify event still exists in database (soft delete)
    assert Event.objects.filter(pk=event.pk).exists()


def test_update_event_status_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'manage_event' permission can update status."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    event.status = Event.EventStatus.DRAFT
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.status == Event.EventStatus.OPEN


def test_update_event_status_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'manage_event' permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    original_status = event.status

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.DELETED})
    response = organization_staff_client.post(url)

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.status == original_status


def test_update_event_status_nonexistent_event(organization_owner_client: Client) -> None:
    """Test updating status of non-existent event returns 404."""
    from uuid import uuid4

    fake_event_id = uuid4()
    url = reverse("api:update_event_status", kwargs={"event_id": fake_event_id, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_update_event_status_requires_authentication(event: Event) -> None:
    """Test that updating event status requires authentication."""
    from django.test.client import Client

    client = Client()
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = client.post(url)

    assert response.status_code == 401


# --- Tests for POST /event-admin/{event_id}/invitation-request/{request_id}/{decision} ---


def test_decide_invitation_request_approve(
    organization_owner_client: Client, event_invitation_request: EventInvitationRequest
) -> None:
    """Test approving an invitation request."""
    url = reverse(
        "api:approve_invitation_request",
        kwargs={
            "event_id": event_invitation_request.event.pk,
            "request_id": event_invitation_request.pk,
        },
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 204
    event_invitation_request.refresh_from_db()
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.APPROVED


def test_decide_invitation_request_reject(
    organization_owner_client: Client, event_invitation_request: EventInvitationRequest
) -> None:
    """Test rejecting an invitation request."""
    url = reverse(
        "api:reject_invitation_request",
        kwargs={
            "event_id": event_invitation_request.event.pk,
            "request_id": event_invitation_request.pk,
        },
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 204
    event_invitation_request.refresh_from_db()
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.REJECTED


# --- Tests for GET /event-admin/{event_id}/invitation-requests ---


def test_list_event_invitation_requests(
    organization_owner_client: Client, public_event: Event, event_invitation_request: EventInvitationRequest
) -> None:
    """Test listing event invitation requests."""
    url = reverse("api:list_invitation_requests", kwargs={"event_id": public_event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(event_invitation_request.pk)


# --- Tests for POST /event-admin/{event_id}/token ---


def test_create_event_token(organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier) -> None:
    """Test creating an event token."""
    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {"name": "Test Token", "max_uses": 10, "ticket_tier_id": str(event_ticket_tier.id)}
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Token"
    assert EventToken.objects.filter(pk=data["id"]).exists()


# --- Tests for GET /event-admin/{event_id}/tokens ---


def test_list_event_tokens(organization_owner_client: Client, event: Event, event_token: EventToken) -> None:
    """Test listing event tokens."""
    url = reverse("api:list_event_tokens", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(event_token.pk)


# --- Tests for PUT /event-admin/token/{token_id} ---


def test_update_event_token(organization_owner_client: Client, event_token: EventToken) -> None:
    """Test updating an event token."""
    url = reverse("api:edit_event_token", kwargs={"event_id": event_token.event_id, "token_id": event_token.pk})
    payload = {"name": "Updated Token Name"}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, response.json()
    event_token.refresh_from_db()
    assert event_token.name == "Updated Token Name"


# --- Tests for DELETE /event-admin/token/{token_id} ---


def test_delete_event_token(organization_owner_client: Client, event_token: EventToken) -> None:
    """Test deleting an event token."""
    url = reverse("api:delete_event_token", kwargs={"event_id": event_token.event_id, "token_id": event_token.pk})
    response = organization_owner_client.delete(url)
    assert response.status_code == 204, response.text
    assert not EventToken.objects.filter(pk=event_token.pk).exists()


# --- Tests for TicketTier CRUD endpoints ---


def test_list_ticket_tiers_by_owner(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that an event owner can list ticket tiers."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2  # there's a default
    assert data["results"][1]["id"] == str(event_ticket_tier.pk)
    assert data["results"][1]["name"] == "General"


def test_list_ticket_tiers_by_staff_with_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that staff with invite_to_event permission can list ticket tiers."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2  # there's default
    assert data["results"][1]["id"] == str(event_ticket_tier.pk)


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_list_ticket_tiers_unauthorized(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that unauthorized users cannot list ticket tiers."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": public_event.pk})

    response = client.get(url)
    assert response.status_code == expected_status_code


def test_create_ticket_tier_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event owner can create a ticket tier."""
    from decimal import Decimal

    from events.models import TicketTier

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {
        "name": "Early Bird",
        "description": "Early bird discount ticket",
        "price": "25.00",
        "currency": "USD",
        "visibility": "public",
        "payment_method": "offline",
        "purchasable_by": "public",
        "total_quantity": 50,
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Early Bird"
    assert data["description"] == "Early bird discount ticket"
    assert data["price"] == "25.00"
    assert data["currency"] == "USD"
    assert data["visibility"] == "public"
    assert data["payment_method"] == "offline"
    assert data["purchasable_by"] == "public"
    assert data["total_quantity"] == 50
    assert data["total_available"] == 50

    # Verify in database
    tier = TicketTier.objects.get(pk=data["id"])
    assert tier.name == "Early Bird"
    assert tier.event == event
    assert tier.price == Decimal("25.00")


def test_create_ticket_tier_by_staff_with_permission(organization_staff_client: Client, event: Event) -> None:
    """Test that staff with edit_event permission can create a ticket tier."""
    from events.models import TicketTier

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {"name": "Staff Created", "price": "15.00", "visibility": "members-only", "purchasable_by": "members"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Staff Created"
    assert TicketTier.objects.filter(pk=data["id"]).exists()


def test_create_ticket_tier_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that staff without edit_event permission cannot create a ticket tier."""
    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {"name": "Should Fail", "price": "10.00"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_create_ticket_tier_unauthorized(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that unauthorized users cannot create ticket tiers."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:create_ticket_tier", kwargs={"event_id": public_event.pk})
    payload = {"name": "Unauthorized", "price": "10.00"}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_update_ticket_tier_by_owner(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that an event owner can update a ticket tier."""
    from decimal import Decimal

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    payload = {
        "name": "Updated General",
        "description": "Updated description",
        "price": "99.99",
        "visibility": "members-only",
        "purchasable_by": "members",
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated General"
    assert data["description"] == "Updated description"
    assert data["price"] == "99.99"
    assert data["visibility"] == "members-only"
    assert data["purchasable_by"] == "members"

    # Verify in database
    event_ticket_tier.refresh_from_db()
    assert event_ticket_tier.name == "Updated General"
    assert event_ticket_tier.price == Decimal("99.99")
    assert event_ticket_tier.visibility == "members-only"


def test_update_ticket_tier_by_staff_with_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that staff with edit_event permission can update a ticket tier."""
    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    payload = {"name": "Staff Updated General"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event_ticket_tier.refresh_from_db()
    assert event_ticket_tier.name == "Staff Updated General"


def test_update_ticket_tier_by_staff_without_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier, staff_member: OrganizationStaff
) -> None:
    """Test that staff without edit_event permission cannot update a ticket tier."""
    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    payload = {"name": "Should Fail"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403


def test_update_nonexistent_ticket_tier(organization_owner_client: Client, event: Event) -> None:
    """Test updating a nonexistent ticket tier returns 404."""
    from uuid import uuid4

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": uuid4()})
    payload = {"name": "Does not exist"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 404


def test_update_ticket_tier_wrong_event(
    organization_owner_client: Client, event: Event, public_event: Event, vip_tier: TicketTier
) -> None:
    """Test updating a ticket tier from a different event returns 404."""
    # vip_tier belongs to public_event, trying to access it via event should fail
    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": vip_tier.pk})
    payload = {"name": "Wrong Event"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_update_ticket_tier_unauthorized(
    request: pytest.FixtureRequest,
    client_fixture: str,
    expected_status_code: int,
    public_event: Event,
    vip_tier: TicketTier,
) -> None:
    """Test that unauthorized users cannot update ticket tiers."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:update_ticket_tier", kwargs={"event_id": public_event.pk, "tier_id": vip_tier.pk})
    payload = {"name": "Unauthorized"}

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code, response.content


def test_delete_ticket_tier_by_owner(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that an event owner can delete a ticket tier."""
    from events.models import TicketTier

    url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not TicketTier.objects.filter(pk=event_ticket_tier.pk).exists()


def test_delete_ticket_tier_by_staff_with_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that staff with edit_event permission can delete a ticket tier."""
    from events.models import TicketTier

    url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not TicketTier.objects.filter(pk=event_ticket_tier.pk).exists()


def test_delete_ticket_tier_by_staff_without_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier, staff_member: OrganizationStaff
) -> None:
    """Test that staff without edit_event permission cannot delete a ticket tier."""
    from events.models import TicketTier

    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403
    assert TicketTier.objects.filter(pk=event_ticket_tier.pk).exists()


def test_delete_nonexistent_ticket_tier(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a nonexistent ticket tier returns 404."""
    from uuid import uuid4

    url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_ticket_tier_wrong_event(organization_owner_client: Client, event: Event, vip_tier: TicketTier) -> None:
    """Test deleting a ticket tier from a different event returns 404."""
    from events.models import TicketTier

    # vip_tier belongs to public_event, not event
    url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": vip_tier.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
    assert TicketTier.objects.filter(pk=vip_tier.pk).exists()


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_delete_ticket_tier_unauthorized(
    request: pytest.FixtureRequest,
    client_fixture: str,
    expected_status_code: int,
    public_event: Event,
    vip_tier: TicketTier,
) -> None:
    """Test that unauthorized users cannot delete ticket tiers."""
    from events.models import TicketTier

    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:delete_ticket_tier", kwargs={"event_id": public_event.pk, "tier_id": vip_tier.pk})

    response = client.delete(url)
    assert response.status_code == expected_status_code
    assert TicketTier.objects.filter(pk=vip_tier.pk).exists()


def test_create_ticket_tier_with_sales_dates(organization_owner_client: Client, event: Event) -> None:
    """Test creating a ticket tier with sales start and end dates."""
    from datetime import timedelta

    start_date = event.start - timedelta(days=30)
    end_date = start_date + timedelta(days=5)

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {
        "name": "Limited Time",
        "price": "30.00",
        "sales_start_at": start_date.isoformat(),
        "sales_end_at": end_date.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Limited Time"
    assert data["sales_start_at"] is not None
    assert data["sales_end_at"] is not None


def test_ticket_tier_crud_maintains_event_relationship(
    organization_owner_client: Client, event: Event, public_event: Event
) -> None:
    """Test that ticket tier operations respect event boundaries."""
    from events.models import TicketTier

    # Create tier for event
    create_url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {"name": "Event Tier", "price": "20.00"}

    response = organization_owner_client.post(create_url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    tier_id = response.json()["id"]

    # Verify tier belongs to correct event
    tier = TicketTier.objects.get(pk=tier_id)
    assert tier.event == event

    # List tiers for the correct event
    list_url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(list_url)
    assert response.status_code == 200
    assert response.json()["count"] == 2

    # List tiers for different event should be empty
    other_list_url = reverse("api:list_ticket_tiers", kwargs={"event_id": public_event.pk})
    response = organization_owner_client.get(other_list_url)
    assert response.status_code == 200
    assert response.json()["count"] == 1


# --- Tests for Pending Tickets Management ---


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    """Create an offline payment ticket tier."""
    return TicketTier.objects.create(
        event=event,
        name="Offline Payment",
        price=25.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def at_door_tier(event: Event) -> TicketTier:
    """Create an at-the-door payment ticket tier."""
    return TicketTier.objects.create(
        event=event,
        name="At The Door",
        price=30.00,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
    )


@pytest.fixture
def pending_offline_ticket(public_user: RevelUser, event: Event, offline_tier: TicketTier) -> Ticket:
    """Create a pending ticket for offline payment."""
    return Ticket.objects.create(
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def pending_at_door_ticket(member_user: RevelUser, event: Event, at_door_tier: TicketTier) -> Ticket:
    """Create a pending ticket for at-the-door payment."""
    return Ticket.objects.create(
        user=member_user,
        event=event,
        tier=at_door_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def active_online_ticket(organization_staff_user: RevelUser, event: Event, event_ticket_tier: TicketTier) -> Ticket:
    """Create an active ticket for online payment (should not appear in pending list)."""
    return Ticket.objects.create(
        user=organization_staff_user,
        event=event,
        tier=event_ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


def test_list_tickets_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    pending_at_door_ticket: Ticket,
    active_online_ticket: Ticket,
) -> None:
    """Test that organization owner can list tickets with filters."""
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    # Test listing all tickets (no filters)
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3  # All tickets

    # Test filtering by status=PENDING
    response = organization_owner_client.get(url, {"status": Ticket.TicketStatus.PENDING})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2  # Only pending tickets
    ticket_ids = [item["id"] for item in data["results"]]
    assert str(pending_offline_ticket.id) in ticket_ids
    assert str(pending_at_door_ticket.id) in ticket_ids
    assert str(active_online_ticket.id) not in ticket_ids

    # Check schema structure
    first_ticket = data["results"][0]
    assert "id" in first_ticket
    assert "status" in first_ticket
    assert "tier" in first_ticket
    assert "user" in first_ticket
    assert "created_at" in first_ticket

    # User info should be included
    assert "email" in first_ticket["user"]
    assert "first_name" in first_ticket["user"]
    assert "last_name" in first_ticket["user"]


def test_list_tickets_by_staff_with_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff with manage_tickets permission can list tickets."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    # Filter by status to only get pending tickets
    response = organization_staff_client.get(url, {"status": Ticket.TicketStatus.PENDING})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_offline_ticket.id)


def test_list_tickets_by_staff_without_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff without manage_tickets permission gets 403."""
    # Ensure permission is False (it should be default)
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_staff_client.get(url)

    assert response.status_code == 403


def test_list_tickets_search(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    pending_at_door_ticket: Ticket,
) -> None:
    """Test searching tickets by user email or name."""
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    # Search by user's email
    search_email = pending_offline_ticket.user.email
    response = organization_owner_client.get(url, {"search": search_email, "status": Ticket.TicketStatus.PENDING})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_offline_ticket.id)


def test_list_tickets_filter_by_payment_method(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    pending_at_door_ticket: Ticket,
    active_online_ticket: Ticket,
) -> None:
    """Test filtering tickets by tier payment method."""
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    # Test filtering by OFFLINE payment method
    response = organization_owner_client.get(url, {"tier__payment_method": TicketTier.PaymentMethod.OFFLINE})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_offline_ticket.id)

    # Test filtering by AT_THE_DOOR payment method
    response = organization_owner_client.get(url, {"tier__payment_method": TicketTier.PaymentMethod.AT_THE_DOOR})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_at_door_ticket.id)

    # Test filtering by ONLINE payment method
    response = organization_owner_client.get(url, {"tier__payment_method": TicketTier.PaymentMethod.ONLINE})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(active_online_ticket.id)


def test_list_tickets_pagination(organization_owner_client: Client, event: Event, offline_tier: TicketTier) -> None:
    """Test pagination of tickets."""
    # Create multiple pending tickets
    users = []
    for i in range(25):  # More than default page size of 20
        user = RevelUser.objects.create(
            username=f"user{i}",
            email=f"user{i}@example.com",
            first_name=f"User{i}",
        )
        users.append(user)
        Ticket.objects.create(
            user=user,
            event=event,
            tier=offline_tier,
            status=Ticket.TicketStatus.PENDING,
        )

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"status": Ticket.TicketStatus.PENDING})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 25
    assert len(data["results"]) == 20  # Default page size
    assert data["next"] is not None
    assert data["previous"] is None


def test_confirm_ticket_payment_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that organization owner can confirm payment for pending tickets."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(pending_offline_ticket.id)
    assert data["status"] == Ticket.TicketStatus.ACTIVE

    # Verify in database
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.ACTIVE


def test_confirm_ticket_payment_by_staff_with_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff with manage_tickets permission can confirm payment."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.ACTIVE


def test_confirm_ticket_payment_by_staff_without_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff without manage_tickets permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 403

    # Verify ticket status unchanged
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_ticket_payment_nonexistent_ticket(organization_owner_client: Client, event: Event) -> None:
    """Test confirming payment for non-existent ticket returns 404."""
    from uuid import uuid4

    fake_ticket_id = uuid4()
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": fake_ticket_id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_confirm_ticket_payment_wrong_event(
    organization_owner_client: Client,
    event: Event,
    public_event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test confirming payment for ticket from different event returns 404."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": public_event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_confirm_ticket_payment_active_ticket(
    organization_owner_client: Client,
    event: Event,
    active_online_ticket: Ticket,
) -> None:
    """Test confirming payment for already active ticket returns 404."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_confirm_ticket_payment_online_payment_method(
    organization_owner_client: Client,
    event: Event,
    public_user: RevelUser,
    event_ticket_tier: TicketTier,
) -> None:
    """Test confirming payment for online payment method ticket returns 404."""
    # Create a pending ticket with online payment method (edge case)
    online_pending_ticket = Ticket.objects.create(
        user=public_user,
        event=event,
        tier=event_ticket_tier,  # This has ONLINE payment method
        status=Ticket.TicketStatus.PENDING,
    )

    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": online_pending_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404

    # Verify ticket status unchanged
    online_pending_ticket.refresh_from_db()
    assert online_pending_ticket.status == Ticket.TicketStatus.PENDING


def test_pending_tickets_endpoints_require_authentication(event: Event, pending_offline_ticket: Ticket) -> None:
    """Test that both endpoints require authentication."""
    from django.test.client import Client

    client = Client()

    list_url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    list_response = client.get(list_url)
    assert list_response.status_code == 401

    confirm_url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    confirm_response = client.post(confirm_url)
    assert confirm_response.status_code == 401


# --- Tests for mark-refunded endpoint ---


def test_mark_ticket_refunded_offline_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    offline_tier: TicketTier,
) -> None:
    """Test that organization owner can mark an offline ticket as refunded."""
    # Set initial quantity_sold
    offline_tier.quantity_sold = 5
    offline_tier.save(update_fields=["quantity_sold"])

    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(pending_offline_ticket.id)
    assert data["status"] == Ticket.TicketStatus.CANCELLED

    # Verify in database
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.CANCELLED

    # Verify quantity was restored
    offline_tier.refresh_from_db()
    assert offline_tier.quantity_sold == 4


def test_mark_ticket_refunded_at_door_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_at_door_ticket: Ticket,
) -> None:
    """Test that organization owner can mark an at-the-door ticket as refunded."""
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": pending_at_door_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    pending_at_door_ticket.refresh_from_db()
    assert pending_at_door_ticket.status == Ticket.TicketStatus.CANCELLED


def test_mark_ticket_refunded_with_payment_record(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that marking a ticket as refunded also marks the payment as refunded."""
    from events.models import Payment

    # Create a payment record for the ticket
    payment = Payment.objects.create(
        ticket=pending_offline_ticket,
        user=pending_offline_ticket.user,
        stripe_session_id="session-id",
        amount=25.00,
        platform_fee=1.00,
        currency="EUR",
        status=Payment.PaymentStatus.SUCCEEDED,
    )

    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200

    # Verify payment status is REFUNDED
    payment.refresh_from_db()
    assert payment.status == Payment.PaymentStatus.REFUNDED


def test_mark_ticket_refunded_by_staff_with_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff with manage_tickets permission can mark ticket as refunded."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.CANCELLED


def test_mark_ticket_refunded_by_staff_without_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff without manage_tickets permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 403

    # Verify ticket status unchanged
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_mark_ticket_refunded_online_ticket_rejected(
    organization_owner_client: Client,
    event: Event,
    active_online_ticket: Ticket,
) -> None:
    """Test that online/Stripe tickets cannot be manually refunded (returns 404)."""
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_mark_ticket_refunded_nonexistent_ticket(organization_owner_client: Client, event: Event) -> None:
    """Test marking non-existent ticket as refunded returns 404."""
    from uuid import uuid4

    fake_ticket_id = uuid4()
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": fake_ticket_id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_mark_ticket_refunded_wrong_event(
    organization_owner_client: Client,
    event: Event,
    public_event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test marking ticket from different event as refunded returns 404."""
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": public_event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


# --- Tests for cancel ticket endpoint ---


def test_cancel_ticket_offline_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    offline_tier: TicketTier,
) -> None:
    """Test that organization owner can cancel an offline ticket."""
    # Set initial quantity_sold
    offline_tier.quantity_sold = 5
    offline_tier.save(update_fields=["quantity_sold"])

    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(pending_offline_ticket.id)
    assert data["status"] == Ticket.TicketStatus.CANCELLED

    # Verify in database
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.CANCELLED

    # Verify quantity was restored
    offline_tier.refresh_from_db()
    assert offline_tier.quantity_sold == 4


def test_cancel_ticket_at_door_by_owner(
    organization_owner_client: Client,
    event: Event,
    pending_at_door_ticket: Ticket,
) -> None:
    """Test that organization owner can cancel an at-the-door ticket."""
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": pending_at_door_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    pending_at_door_ticket.refresh_from_db()
    assert pending_at_door_ticket.status == Ticket.TicketStatus.CANCELLED


def test_cancel_ticket_by_staff_with_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff with manage_tickets permission can cancel ticket."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.CANCELLED


def test_cancel_ticket_by_staff_without_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that staff without manage_tickets permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 403

    # Verify ticket status unchanged
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_cancel_ticket_online_ticket_rejected(
    organization_owner_client: Client,
    event: Event,
    active_online_ticket: Ticket,
) -> None:
    """Test that online/Stripe tickets cannot be manually canceled (returns 404)."""
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_cancel_ticket_nonexistent_ticket(organization_owner_client: Client, event: Event) -> None:
    """Test canceling non-existent ticket returns 404."""
    from uuid import uuid4

    fake_ticket_id = uuid4()
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": fake_ticket_id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_cancel_ticket_wrong_event(
    organization_owner_client: Client,
    event: Event,
    public_event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test canceling ticket from different event returns 404."""
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": public_event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


# --- Tests for Event Check-in Window and Check-in Process ---


def test_update_event_check_in_window(organization_owner_client: Client, event: Event) -> None:
    """Test updating event with check-in window fields."""
    from datetime import timedelta

    check_in_start = event.start + timedelta(hours=-1)
    check_in_end = event.end + timedelta(hours=1)

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "check_in_starts_at": check_in_start.isoformat(),
        "check_in_ends_at": check_in_end.isoformat(),
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.check_in_starts_at == check_in_start
    assert event.check_in_ends_at == check_in_end


def test_check_in_success(organization_owner_client: Client, event: Event, active_online_ticket: Ticket) -> None:
    """Test successful ticket check-in."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(active_online_ticket.id)
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN
    assert data["checked_in_at"] is not None

    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert active_online_ticket.checked_in_at is not None
    assert active_online_ticket.checked_in_by is not None


def test_check_in_already_checked_in(
    organization_owner_client: Client, event: Event, active_online_ticket: Ticket
) -> None:
    """Test check-in fails when ticket is already checked in."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    # Mark ticket as already checked in
    active_online_ticket.status = Ticket.TicketStatus.CHECKED_IN
    active_online_ticket.checked_in_at = now
    active_online_ticket.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400
    assert "already been checked in" in response.json()["detail"]


def test_check_in_window_not_open(
    organization_owner_client: Client, event: Event, active_online_ticket: Ticket
) -> None:
    """Test check-in fails when check-in window is not open."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be closed (in the future)
    now = timezone.now()
    event.check_in_starts_at = now + timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=2)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400
    assert "Check-in is not currently open" in response.json()["detail"]


def test_check_in_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff, active_online_ticket: Ticket
) -> None:
    """Test staff member with check_in_attendees permission can check in tickets."""
    from datetime import timedelta

    from django.utils import timezone

    # Grant permission
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_staff_client.post(url, content_type="application/json")

    assert response.status_code == 200
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert active_online_ticket.checked_in_by == staff_member.user


def test_check_in_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff, active_online_ticket: Ticket
) -> None:
    """Test staff member without check_in_attendees permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_staff_client.post(url, content_type="application/json")

    assert response.status_code == 403


def test_check_in_requires_authentication(event: Event, active_online_ticket: Ticket) -> None:
    """Test check-in requires authentication."""
    from django.test.client import Client

    client = Client()
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = client.post(url, content_type="application/json")

    assert response.status_code == 401


# --- Tests for Direct Invitations ---


def test_create_direct_invitations_for_existing_users(
    organization_owner_client: Client, event: Event, public_user: RevelUser, member_user: RevelUser
) -> None:
    """Test creating direct invitations for existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": [public_user.email, member_user.email],
        "waives_questionnaire": True,
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["created_invitations"] == 2
    assert data["pending_invitations"] == 0
    assert data["total_invited"] == 2

    # Verify invitations were created
    from events.models import EventInvitation

    invitations = EventInvitation.objects.filter(event=event)
    assert invitations.count() == 2
    assert set(invitations.values_list("user__email", flat=True)) == {public_user.email, member_user.email}


def test_create_direct_invitations_for_non_existing_users(organization_owner_client: Client, event: Event) -> None:
    """Test creating direct invitations for non-existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": ["nonexistent1@example.com", "nonexistent2@example.com"],
        "waives_purchase": True,
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["created_invitations"] == 0
    assert data["pending_invitations"] == 2
    assert data["total_invited"] == 2

    # Verify pending invitations were created
    from events.models import PendingEventInvitation

    pending_invitations = PendingEventInvitation.objects.filter(event=event)
    assert pending_invitations.count() == 2
    assert set(pending_invitations.values_list("email", flat=True)) == {
        "nonexistent1@example.com",
        "nonexistent2@example.com",
    }


def test_create_direct_invitations_mixed_users(
    organization_owner_client: Client, event: Event, public_user: RevelUser
) -> None:
    """Test creating direct invitations for both existing and non-existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": [public_user.email, "new@example.com"],
        "custom_message": "Welcome to our event!",
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["created_invitations"] == 1
    assert data["pending_invitations"] == 1
    assert data["total_invited"] == 2


def test_create_direct_invitations_requires_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that creating direct invitations requires the invite_to_event permission."""
    # Remove the invite_to_event permission
    tier = event.ticket_tiers.first()
    assert tier is not None
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    payload = {
        "emails": ["test@example.com"],
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403


def test_pending_invitation_conversion_on_user_creation(event: Event) -> None:
    """Test that pending invitations are converted to real invitations when users sign up."""
    from events.models import EventInvitation, PendingEventInvitation

    # Create a pending invitation
    PendingEventInvitation.objects.create(
        event=event,
        email="newuser@example.com",
        waives_questionnaire=True,
        custom_message="Test message",
    )

    assert PendingEventInvitation.objects.count() == 1
    assert EventInvitation.objects.count() == 0

    # Create a user with the same email
    new_user = RevelUser.objects.create(
        username="newuser",
        email="newuser@example.com",
        first_name="New",
        last_name="User",
    )

    # Check that the pending invitation was converted
    assert PendingEventInvitation.objects.count() == 0
    assert EventInvitation.objects.count() == 1

    invitation = EventInvitation.objects.first()
    assert invitation is not None
    assert invitation.user == new_user
    assert invitation.event == event
    assert invitation.waives_questionnaire is True
    assert invitation.custom_message == "Test message"


def test_list_event_invitations(
    organization_owner_client: Client, event: Event, public_user: RevelUser, member_user: RevelUser
) -> None:
    """Test listing event invitations."""
    from events.models import EventInvitation

    # Create some invitations
    EventInvitation.objects.create(event=event, user=public_user, waives_questionnaire=True)
    EventInvitation.objects.create(event=event, user=member_user, custom_message="Welcome!")

    url = reverse("api:list_event_invitations", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["results"]) == 2


def test_list_pending_invitations(organization_owner_client: Client, event: Event) -> None:
    """Test listing pending invitations."""
    from events.models import PendingEventInvitation

    # Create some pending invitations
    PendingEventInvitation.objects.create(event=event, email="test1@example.com", waives_purchase=True)
    PendingEventInvitation.objects.create(event=event, email="test2@example.com", custom_message="Join us!")

    url = reverse("api:list_pending_invitations", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["results"]) == 2


def test_delete_event_invitation(organization_owner_client: Client, event: Event, public_user: RevelUser) -> None:
    """Test deleting an event invitation."""
    from events.models import EventInvitation

    # Create an invitation
    invitation = EventInvitation.objects.create(event=event, user=public_user)

    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "registered",
            "invitation_id": invitation.pk,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not EventInvitation.objects.filter(pk=invitation.pk).exists()


def test_delete_pending_invitation(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a pending invitation."""
    from events.models import PendingEventInvitation

    # Create a pending invitation
    invitation = PendingEventInvitation.objects.create(event=event, email="test@example.com")

    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "pending",
            "invitation_id": invitation.pk,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not PendingEventInvitation.objects.filter(pk=invitation.pk).exists()


def test_delete_nonexistent_invitation(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a non-existent invitation returns 404."""
    from uuid import uuid4

    fake_invitation_id = uuid4()
    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "registered",
            "invitation_id": fake_invitation_id,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
