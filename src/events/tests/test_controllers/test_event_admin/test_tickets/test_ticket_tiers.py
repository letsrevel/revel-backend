"""Tests for TicketTier CRUD endpoints and membership restrictions."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationStaff, TicketTier

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
    tier_ids = {r["id"] for r in data["results"]}
    assert str(event_ticket_tier.pk) in tier_ids


def test_list_ticket_tiers_by_staff_with_permission(
    organization_staff_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that staff with invite_to_event permission can list ticket tiers."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2  # there's default
    tier_ids = {r["id"] for r in data["results"]}
    assert str(event_ticket_tier.pk) in tier_ids


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
