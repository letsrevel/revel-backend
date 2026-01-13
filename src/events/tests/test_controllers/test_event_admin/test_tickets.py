"""Tests for ticket tier and ticket management endpoints."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


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


# --- Tests for Ticket Tier Membership Restrictions ---


def test_create_ticket_tier_with_membership_tier_restrictions(
    organization_owner_client: Client, event: Event, organization: Organization
) -> None:
    """Test creating a ticket tier with membership tier restrictions."""
    from events.models import MembershipTier, TicketTier

    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {
        "name": "VIP Ticket",
        "price": "100.00",
        "visibility": "public",
        "payment_method": "offline",
        "purchasable_by": "members",
        "restricted_to_membership_tiers_ids": [str(gold_tier.id), str(silver_tier.id)],
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "VIP Ticket"
    assert data["restricted_to_membership_tiers"] is not None
    assert len(data["restricted_to_membership_tiers"]) == 2

    # Verify in database
    tier = TicketTier.objects.get(pk=data["id"])
    assert tier.restricted_to_membership_tiers.count() == 2
    tier_ids = set(tier.restricted_to_membership_tiers.values_list("id", flat=True))
    assert gold_tier.id in tier_ids
    assert silver_tier.id in tier_ids


def test_create_ticket_tier_with_invalid_membership_tier_id(organization_owner_client: Client, event: Event) -> None:
    """Test creating ticket tier with non-existent membership tier ID fails."""
    from uuid import uuid4

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {
        "name": "Invalid Tier",
        "price": "50.00",
        "purchasable_by": "members",
        "restricted_to_membership_tiers_ids": [str(uuid4())],  # Non-existent ID
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_create_ticket_tier_with_wrong_organization_membership_tier(
    organization_owner_client: Client, event: Event, organization_owner_user: RevelUser
) -> None:
    """Test creating ticket tier with membership tier from different organization fails."""
    from events.models import MembershipTier, Organization

    # Create another organization with a tier
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {
        "name": "Cross-Org Tier",
        "price": "50.00",
        "purchasable_by": "members",
        "restricted_to_membership_tiers_ids": [str(other_tier.id)],
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_update_ticket_tier_add_membership_restrictions(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, organization: Organization
) -> None:
    """Test updating a ticket tier to add membership tier restrictions."""
    from events.models import MembershipTier

    # Create membership tier
    platinum_tier = MembershipTier.objects.create(organization=organization, name="Platinum")

    # Initially no restrictions
    assert event_ticket_tier.restricted_to_membership_tiers.count() == 0

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
    payload = {
        "purchasable_by": "members",
        "restricted_to_membership_tiers_ids": [str(platinum_tier.id)],
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert len(data["restricted_to_membership_tiers"]) == 1

    # Verify in database
    event_ticket_tier.refresh_from_db()
    assert event_ticket_tier.restricted_to_membership_tiers.count() == 1
    assert event_ticket_tier.restricted_to_membership_tiers.first() == platinum_tier


def test_update_ticket_tier_replace_membership_restrictions(
    organization_owner_client: Client, event: Event, organization: Organization
) -> None:
    """Test updating a ticket tier to replace existing membership tier restrictions."""
    from events.models import MembershipTier, TicketTier

    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")
    bronze_tier = MembershipTier.objects.create(organization=organization, name="Bronze")

    # Create tier with initial restrictions (must have purchasable_by=MEMBERS for restrictions)
    tier = TicketTier.objects.create(
        event=event,
        name="Restricted",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )
    tier.restricted_to_membership_tiers.add(gold_tier, silver_tier)

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": tier.pk})
    payload = {
        "restricted_to_membership_tiers_ids": [str(bronze_tier.id)],
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert len(data["restricted_to_membership_tiers"]) == 1
    assert data["restricted_to_membership_tiers"][0]["name"] == "Bronze"

    # Verify in database
    tier.refresh_from_db()
    assert tier.restricted_to_membership_tiers.count() == 1
    assert tier.restricted_to_membership_tiers.first() == bronze_tier


def test_update_ticket_tier_clear_membership_restrictions(
    organization_owner_client: Client, event: Event, organization: Organization
) -> None:
    """Test updating a ticket tier to clear all membership tier restrictions."""
    from events.models import MembershipTier, TicketTier

    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create tier with restrictions (must have purchasable_by=MEMBERS for restrictions)
    tier = TicketTier.objects.create(
        event=event,
        name="Restricted",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )
    tier.restricted_to_membership_tiers.add(gold_tier)

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": tier.pk})
    payload: dict[str, list[str]] = {
        "restricted_to_membership_tiers_ids": [],  # Empty list to clear
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    # The response should show empty list or None for restricted tiers
    assert data["restricted_to_membership_tiers"] is None or data["restricted_to_membership_tiers"] == []

    # Verify in database
    tier.refresh_from_db()
    assert tier.restricted_to_membership_tiers.count() == 0


def test_update_ticket_tier_preserve_membership_restrictions_when_not_provided(
    organization_owner_client: Client, event: Event, organization: Organization
) -> None:
    """Test that not providing restricted_to_membership_tiers_ids preserves existing restrictions."""
    from events.models import MembershipTier, TicketTier

    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create tier with restrictions (must have purchasable_by=MEMBERS for restrictions)
    tier = TicketTier.objects.create(
        event=event,
        name="Restricted",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )
    tier.restricted_to_membership_tiers.add(gold_tier)

    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": tier.pk})
    payload = {
        "name": "Updated Name",
        # Not providing restricted_to_membership_tiers_ids
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Updated Name"
    assert len(data["restricted_to_membership_tiers"]) == 1

    # Verify restrictions are preserved
    tier.refresh_from_db()
    assert tier.restricted_to_membership_tiers.count() == 1
    assert tier.restricted_to_membership_tiers.first() == gold_tier


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
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def pending_at_door_ticket(member_user: RevelUser, event: Event, at_door_tier: TicketTier) -> Ticket:
    """Create a pending ticket for at-the-door payment."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=member_user,
        event=event,
        tier=at_door_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def active_online_ticket(organization_staff_user: RevelUser, event: Event, event_ticket_tier: TicketTier) -> Ticket:
    """Create an active ticket for online payment (should not appear in pending list)."""
    return Ticket.objects.create(
        guest_name="Test Guest",
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
            guest_name="Test Guest",
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
        guest_name="Test Guest",
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


# --- Tests for membership field in list endpoints ---


def test_list_tickets_membership_null_for_non_member(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    nonmember_user: RevelUser,
) -> None:
    """Test that ticket list returns membership=null for non-members."""
    # Create ticket for non-member user
    ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=nonmember_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"status": Ticket.TicketStatus.PENDING})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1

    # Find our ticket in the results
    ticket_data = next((t for t in data["results"] if t["id"] == str(ticket.id)), None)
    assert ticket_data is not None
    assert ticket_data["membership"] is None


def test_list_tickets_membership_present_for_member(
    organization_owner_client: Client,
    organization: Organization,
    event: Event,
    offline_tier: TicketTier,
    nonmember_user: RevelUser,
) -> None:
    """Test that ticket list returns membership object for organization members."""
    # Create membership tier and make user a member
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    membership = OrganizationMember.objects.create(organization=organization, user=nonmember_user, tier=tier)

    # Create ticket for member user
    ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=nonmember_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"status": Ticket.TicketStatus.PENDING})

    assert response.status_code == 200
    data = response.json()

    # Find our ticket in the results
    ticket_data = next((t for t in data["results"] if t["id"] == str(ticket.id)), None)
    assert ticket_data is not None
    assert ticket_data["membership"] is not None
    assert ticket_data["membership"]["status"] == membership.status
    assert ticket_data["membership"]["tier"]["name"] == tier.name
