"""Tests for GET /events/{event_id}/my-status endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    Organization,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


def test_get_my_event_status_with_ticket(
    nonmember_client: Client, nonmember_user: RevelUser, public_event: Event
) -> None:
    """Test status returns a ticket if one exists for the user."""
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    ticket = Ticket.objects.create(guest_name="Test Guest", event=public_event, user=nonmember_user, tier=tier)
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # Response now returns tickets list with per-tier purchase limits
    assert len(data["tickets"]) == 1
    assert data["tickets"][0]["id"] == str(ticket.id)
    assert data["tickets"][0]["status"] == "active"
    assert data["can_purchase_more"] is False  # max_tickets_per_user defaults to 1
    # remaining_tickets is now a list of per-tier remaining counts
    assert isinstance(data["remaining_tickets"], list)
    assert len(data["remaining_tickets"]) == 1
    assert data["remaining_tickets"][0]["tier_id"] == str(tier.id)
    assert data["remaining_tickets"][0]["remaining"] == 0  # user has 1, max is 1
    assert data["remaining_tickets"][0]["sold_out"] is False  # tier has unlimited inventory


def test_get_my_event_status_with_rsvp(
    nonmember_client: Client, nonmember_user: RevelUser, rsvp_only_public_event: Event
) -> None:
    """Test status returns an RSVP if one exists for the user."""
    rsvp = EventRSVP.objects.create(event=rsvp_only_public_event, user=nonmember_user, status="yes")
    url = reverse("api:get_my_event_status", kwargs={"event_id": rsvp_only_public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # Response now wraps rsvp in EventUserStatusResponse
    assert data["rsvp"]["status"] == rsvp.status
    assert data["rsvp"]["event_id"] == str(rsvp_only_public_event.pk)
    assert data["tickets"] == []
    assert data["remaining_tickets"] == []  # empty list for RSVP events


def test_get_my_event_status_is_eligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is eligible but has no ticket/rsvp."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["event_id"] == str(public_event.pk)


def test_get_my_event_status_is_ineligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is ineligible."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200  # The endpoint itself succeeds, it returns the status
    data = response.json()
    assert data["allowed"] is True


def test_get_my_event_status_anonymous(client: Client, public_event: Event) -> None:
    """Test anonymous user gets 401."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = client.get(url)
    assert response.status_code == 401


def test_get_my_event_status_multi_tier(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    public_event: Event,
) -> None:
    """Test status returns per-tier remaining counts for multiple eligible tiers.

    This tests the core multi-tier functionality where a user can see different
    tiers with different remaining counts.
    """
    # Get the auto-created default tier
    default_tier = public_event.ticket_tiers.first()
    assert default_tier is not None
    default_tier.max_tickets_per_user = 2  # Allow 2 tickets per user
    default_tier.save()

    # Create a second public tier with different limits
    second_tier = TicketTier.objects.create(
        event=public_event,
        name="Second Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.PUBLIC,
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        max_tickets_per_user=3,  # Allow 3 tickets per user
    )

    # User purchases 1 ticket from default tier
    Ticket.objects.create(
        event=public_event,
        user=nonmember_user,
        tier=default_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Should have 1 ticket
    assert len(data["tickets"]) == 1
    assert data["tickets"][0]["tier"]["id"] == str(default_tier.id)

    # Should have 2 tiers in remaining_tickets
    assert len(data["remaining_tickets"]) == 2

    # Find the remaining counts by tier_id
    remaining_by_tier = {r["tier_id"]: r for r in data["remaining_tickets"]}

    # Default tier: max 2, user has 1 -> remaining = 1
    assert str(default_tier.id) in remaining_by_tier
    assert remaining_by_tier[str(default_tier.id)]["remaining"] == 1
    assert remaining_by_tier[str(default_tier.id)]["sold_out"] is False

    # Second tier: max 3, user has 0 -> remaining = 3
    assert str(second_tier.id) in remaining_by_tier
    assert remaining_by_tier[str(second_tier.id)]["remaining"] == 3
    assert remaining_by_tier[str(second_tier.id)]["sold_out"] is False

    # can_purchase_more should be True (both tiers have remaining > 0)
    assert data["can_purchase_more"] is True


def test_get_my_event_status_multi_tier_with_sold_out(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
) -> None:
    """Test status returns sold_out=True for tiers that have no inventory."""
    # Get the auto-created default tier and make it sold out
    sold_out_tier = public_event.ticket_tiers.first()
    assert sold_out_tier is not None
    sold_out_tier.total_quantity = 5
    sold_out_tier.quantity_sold = 5  # Completely sold out
    sold_out_tier.max_tickets_per_user = 2
    sold_out_tier.save()

    # Create a second tier with available inventory
    available_tier = TicketTier.objects.create(
        event=public_event,
        name="Available Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.PUBLIC,
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        total_quantity=10,
        quantity_sold=3,  # 7 remaining
        max_tickets_per_user=5,
    )

    # User purchases 1 ticket from the available tier (so they have status)
    Ticket.objects.create(
        event=public_event,
        user=nonmember_user,
        tier=available_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Should have 2 tiers in remaining_tickets
    assert len(data["remaining_tickets"]) == 2

    remaining_by_tier = {r["tier_id"]: r for r in data["remaining_tickets"]}

    # Sold out tier: user has remaining quota (2) but tier is sold out
    assert str(sold_out_tier.id) in remaining_by_tier
    assert remaining_by_tier[str(sold_out_tier.id)]["remaining"] == 2  # User's personal limit
    assert remaining_by_tier[str(sold_out_tier.id)]["sold_out"] is True  # But tier is sold out

    # Available tier: max 5, user has 1 -> remaining = 4, not sold out
    assert str(available_tier.id) in remaining_by_tier
    assert remaining_by_tier[str(available_tier.id)]["remaining"] == 4
    assert remaining_by_tier[str(available_tier.id)]["sold_out"] is False


def test_get_my_event_status_all_tiers_sold_out(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
) -> None:
    """Test can_purchase_more is False when all eligible tiers are sold out.

    This is a regression test for a bug where can_purchase_more only checked
    remaining quota but not sold_out status. A user could have personal quota
    remaining but still cannot purchase if all tiers are sold out.
    """
    # Get the auto-created default tier and make it sold out
    sold_out_tier = public_event.ticket_tiers.first()
    assert sold_out_tier is not None
    sold_out_tier.total_quantity = 5
    sold_out_tier.quantity_sold = 5  # Completely sold out
    sold_out_tier.max_tickets_per_user = 3  # User has quota remaining
    sold_out_tier.save()

    # User purchases 1 ticket (so they have status) - purchased before sellout
    Ticket.objects.create(
        event=public_event,
        user=nonmember_user,
        tier=sold_out_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # User has personal remaining quota (max 3, has 1 -> remaining 2)
    assert len(data["remaining_tickets"]) == 1
    assert data["remaining_tickets"][0]["remaining"] == 2
    assert data["remaining_tickets"][0]["sold_out"] is True

    # But can_purchase_more should be False because the tier is sold out
    assert data["can_purchase_more"] is False


def test_get_my_event_status_multi_tier_with_unlimited(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
) -> None:
    """Test status correctly handles tiers with unlimited quantities."""
    # Set event-level limits to None/0 so tier can truly be unlimited
    public_event.max_tickets_per_user = None
    public_event.max_attendees = 0  # 0 means unlimited capacity
    public_event.save()

    # Get the auto-created default tier
    limited_tier = public_event.ticket_tiers.first()
    assert limited_tier is not None
    limited_tier.max_tickets_per_user = 2  # Tier-level override
    limited_tier.save()

    # Create an unlimited tier (no max_tickets_per_user, inherits event's None)
    unlimited_tier = TicketTier.objects.create(
        event=public_event,
        name="Unlimited Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.PUBLIC,
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        max_tickets_per_user=None,  # Inherits event's unlimited
        total_quantity=None,  # Unlimited inventory
    )

    # User purchases 1 ticket from limited tier (so they have status)
    Ticket.objects.create(
        event=public_event,
        user=nonmember_user,
        tier=limited_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    remaining_by_tier = {r["tier_id"]: r for r in data["remaining_tickets"]}

    # Limited tier: max 2, user has 1 -> remaining = 1
    assert remaining_by_tier[str(limited_tier.id)]["remaining"] == 1
    assert remaining_by_tier[str(limited_tier.id)]["sold_out"] is False

    # Unlimited tier: remaining = None (unlimited)
    assert remaining_by_tier[str(unlimited_tier.id)]["remaining"] is None
    assert remaining_by_tier[str(unlimited_tier.id)]["sold_out"] is False

    # can_purchase_more should be True
    assert data["can_purchase_more"] is True


def test_get_my_event_status_multi_tier_members_only_visibility(
    member_client: Client,
    member_user: RevelUser,
    organization: Organization,
    public_event: Event,
) -> None:
    """Test that members can see MEMBERS_ONLY visibility tiers."""
    # Get the auto-created default tier (public)
    public_tier = public_event.ticket_tiers.first()
    assert public_tier is not None

    # Create a members-only tier
    members_tier = TicketTier.objects.create(
        event=public_event,
        name="Members Only Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
        max_tickets_per_user=5,
    )

    # User (who is a member via member_client fixture) purchases from public tier
    Ticket.objects.create(
        event=public_event,
        user=member_user,
        tier=public_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = member_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Member should see both tiers
    assert len(data["remaining_tickets"]) == 2

    tier_ids = {r["tier_id"] for r in data["remaining_tickets"]}
    assert str(public_tier.id) in tier_ids
    assert str(members_tier.id) in tier_ids


def test_get_my_event_status_nonmember_cannot_see_members_only_tier(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
) -> None:
    """Test that non-members cannot see MEMBERS_ONLY visibility tiers."""
    # Get the auto-created default tier (public)
    public_tier = public_event.ticket_tiers.first()
    assert public_tier is not None

    # Create a members-only tier
    TicketTier.objects.create(
        event=public_event,
        name="Members Only Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
        max_tickets_per_user=5,
    )

    # Non-member purchases from public tier
    Ticket.objects.create(
        event=public_event,
        user=nonmember_user,
        tier=public_tier,
        guest_name="Guest 1",
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Non-member should only see the public tier
    assert len(data["remaining_tickets"]) == 1
    assert data["remaining_tickets"][0]["tier_id"] == str(public_tier.id)
