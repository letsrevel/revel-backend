"""Tests for ticket operations (list, confirm, check-in, unconfirm)."""

import json
from decimal import Decimal

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    OrganizationStaff,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


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
    response = organization_owner_client.post(url, content_type="application/json")

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
    response = organization_staff_client.post(url, content_type="application/json")

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
    response = organization_staff_client.post(url, content_type="application/json")

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
    response = organization_owner_client.post(url, content_type="application/json")

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
    response = organization_owner_client.post(url, content_type="application/json")

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
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 404


def test_confirm_ticket_payment_already_active_offline_ticket(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test confirming payment for an already-active offline ticket returns 404.

    The status=PENDING filter ensures admins cannot double-confirm tickets.
    """
    active_offline_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_offline_ticket.pk},
    )
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 404

    # Verify ticket status unchanged
    active_offline_ticket.refresh_from_db()
    assert active_offline_ticket.status == Ticket.TicketStatus.ACTIVE


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
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 404

    # Verify ticket status unchanged
    online_pending_ticket.refresh_from_db()
    assert online_pending_ticket.status == Ticket.TicketStatus.PENDING


# --- Tests for unconfirm ticket payment endpoint ---


def test_unconfirm_ticket_payment_by_owner(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test that organization owner can unconfirm payment for active offline tickets."""
    # Create an active offline ticket
    active_offline_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(active_offline_ticket.id)
    assert data["status"] == Ticket.TicketStatus.PENDING

    # Verify in database
    active_offline_ticket.refresh_from_db()
    assert active_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_unconfirm_ticket_payment_clears_price_paid(
    organization_owner_client: Client,
    event: Event,
    pwyc_offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test that unconfirming a PWYC ticket clears price_paid."""
    # Create an active PWYC offline ticket with price_paid set
    ticket = Ticket.objects.create(
        guest_name="PWYC Guest",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
        price_paid=Decimal("15.00"),
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.PENDING
    assert data["price_paid"] is None

    # Verify in database
    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.PENDING
    assert ticket.price_paid is None


def test_unconfirm_and_reconfirm_pwyc_round_trip(
    organization_owner_client: Client,
    event: Event,
    pwyc_offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test full lifecycle: confirm with price -> unconfirm -> confirm again with different price."""
    ticket = Ticket.objects.create(
        guest_name="PWYC Guest",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )

    confirm_url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
    )
    unconfirm_url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
    )

    # Step 1: Confirm with price_paid=15
    response = organization_owner_client.post(
        confirm_url,
        data=json.dumps({"price_paid": "15.00"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    ticket = Ticket.objects.get(pk=ticket.pk)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.price_paid == Decimal("15.00")

    # Step 2: Unconfirm — should clear price_paid
    response = organization_owner_client.post(unconfirm_url)
    assert response.status_code == 200
    ticket = Ticket.objects.get(pk=ticket.pk)
    assert ticket.status == Ticket.TicketStatus.PENDING
    assert ticket.price_paid is None

    # Step 3: Confirm again with a different price
    response = organization_owner_client.post(
        confirm_url,
        data=json.dumps({"price_paid": "25.00"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    ticket = Ticket.objects.get(pk=ticket.pk)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.price_paid == Decimal("25.00")


def test_unconfirm_ticket_payment_at_door_rejected(
    organization_owner_client: Client,
    event: Event,
    at_door_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test that unconfirm is rejected for AT_THE_DOOR tickets.

    AT_THE_DOOR tickets are always ACTIVE (commitment to attend) and should
    not be reverted to PENDING.
    """
    # Create an active at-the-door ticket
    active_at_door_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=at_door_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_at_door_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404

    # Verify ticket status unchanged
    active_at_door_ticket.refresh_from_db()
    assert active_at_door_ticket.status == Ticket.TicketStatus.ACTIVE


def test_unconfirm_ticket_payment_by_staff_with_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test that staff with manage_tickets permission can unconfirm payment."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Create an active offline ticket
    active_offline_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    active_offline_ticket.refresh_from_db()
    assert active_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_unconfirm_ticket_payment_by_staff_without_permission(
    organization_staff_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test that staff without manage_tickets permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    # Create an active offline ticket
    active_offline_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_offline_ticket.pk},
    )
    response = organization_staff_client.post(url)

    assert response.status_code == 403

    # Verify ticket status unchanged
    active_offline_ticket.refresh_from_db()
    assert active_offline_ticket.status == Ticket.TicketStatus.ACTIVE


def test_unconfirm_ticket_payment_nonexistent_ticket(organization_owner_client: Client, event: Event) -> None:
    """Test unconfirming payment for non-existent ticket returns 404."""
    from uuid import uuid4

    fake_ticket_id = uuid4()
    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": fake_ticket_id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_unconfirm_ticket_payment_wrong_event(
    organization_owner_client: Client,
    event: Event,
    public_event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test unconfirming payment for ticket from different event returns 404."""
    # Create an active offline ticket for the main event
    active_offline_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": public_event.pk, "ticket_id": active_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_unconfirm_ticket_payment_pending_ticket(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test unconfirming payment for already pending ticket returns 404."""
    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_unconfirm_ticket_payment_checked_in_ticket(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test unconfirming payment for checked-in ticket returns 404."""
    # Create a checked-in offline ticket
    checked_in_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.CHECKED_IN,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": checked_in_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_unconfirm_ticket_payment_cancelled_ticket(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """Test unconfirming payment for cancelled ticket returns 404."""
    # Create a cancelled offline ticket
    cancelled_ticket = Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.CANCELLED,
    )

    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": cancelled_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_unconfirm_ticket_payment_online_payment_method(
    organization_owner_client: Client,
    event: Event,
    active_online_ticket: Ticket,
) -> None:
    """Test unconfirming payment for online payment method ticket returns 404."""
    url = reverse(
        "api:unconfirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404

    # Verify ticket status unchanged
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.ACTIVE


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
