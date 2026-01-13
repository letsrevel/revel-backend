"""Tests for ticket operations (pending tickets, refund, cancel, membership field)."""

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
