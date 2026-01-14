"""Tests for GET /events/{event_id}/dietary-summary endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


def test_get_dietary_summary_as_organizer(
    organization_owner_client: Client,
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """Test that event organizer can view dietary summary with all dietary info."""
    from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, UserDietaryPreference

    # Create some attendees with dietary needs
    attendee1 = RevelUser.objects.create_user(
        username="attendee1@test.com", email="attendee1@test.com", password="pass", first_name="Alice"
    )
    attendee2 = RevelUser.objects.create_user(
        username="attendee2@test.com", email="attendee2@test.com", password="pass", first_name="Bob"
    )

    # Create tickets for attendees
    tier = TicketTier.objects.create(
        event=event,
        name="General",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE,
        price=0,
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee2, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )

    # Add dietary restrictions
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    gluten, _ = FoodItem.objects.get_or_create(name="Gluten")
    DietaryRestriction.objects.create(
        user=attendee1,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Carries EpiPen",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee2,
        food_item=gluten,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Celiac disease",
        is_public=False,  # Private restriction
    )

    # Add dietary preferences
    vegan = DietaryPreference.objects.get(name="Vegan")
    UserDietaryPreference.objects.create(user=attendee1, preference=vegan, comment="Strict vegan", is_public=True)

    url = reverse("api:event_dietary_summary", args=[event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Organizer should see both public and private restrictions
    assert len(data["restrictions"]) == 2
    restrictions_by_food = {r["food_item"]: r for r in data["restrictions"]}
    assert "Peanuts" in restrictions_by_food
    assert "Gluten" in restrictions_by_food
    assert restrictions_by_food["Peanuts"]["severity"] == "severe_allergy"
    assert restrictions_by_food["Peanuts"]["attendee_count"] == 1
    assert restrictions_by_food["Gluten"]["severity"] == "allergy"
    assert restrictions_by_food["Gluten"]["attendee_count"] == 1

    # Check preferences
    assert len(data["preferences"]) == 1
    assert data["preferences"][0]["name"] == "Vegan"
    assert data["preferences"][0]["attendee_count"] == 1


def test_get_dietary_summary_as_regular_attendee(
    nonmember_client: Client,
    event: Event,
    nonmember_user: RevelUser,
) -> None:
    """Test that regular attendee sees only public dietary info."""
    from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, UserDietaryPreference

    # Create some attendees with dietary needs
    attendee1 = RevelUser.objects.create_user(
        username="attendee1@test.com", email="attendee1@test.com", password="pass", first_name="Alice"
    )
    attendee2 = RevelUser.objects.create_user(
        username="attendee2@test.com", email="attendee2@test.com", password="pass", first_name="Bob"
    )

    # Create tickets for all users including the requesting user
    tier = TicketTier.objects.create(
        event=event,
        name="General",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE,
        price=0,
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=nonmember_user, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee2, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )

    # Add dietary restrictions
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    shellfish, _ = FoodItem.objects.get_or_create(name="Shellfish")
    DietaryRestriction.objects.create(
        user=attendee1,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Public allergy",
        is_public=True,  # Public
    )
    DietaryRestriction.objects.create(
        user=attendee2,
        food_item=shellfish,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Private allergy",
        is_public=False,  # Private
    )

    # Add dietary preferences
    vegan = DietaryPreference.objects.get(name="Vegan")
    vegetarian = DietaryPreference.objects.get(name="Vegetarian")
    UserDietaryPreference.objects.create(user=attendee1, preference=vegan, comment="Strict vegan", is_public=True)
    UserDietaryPreference.objects.create(
        user=attendee2, preference=vegetarian, comment="Private preference", is_public=False
    )

    url = reverse("api:event_dietary_summary", args=[event.id])
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Regular attendee should only see public restrictions
    assert len(data["restrictions"]) == 1
    assert data["restrictions"][0]["food_item"] == "Peanuts"
    assert data["restrictions"][0]["severity"] == "allergy"
    assert data["restrictions"][0]["attendee_count"] == 1

    # Should only see public preferences
    assert len(data["preferences"]) == 1
    assert data["preferences"][0]["name"] == "Vegan"
    assert data["preferences"][0]["attendee_count"] == 1


def test_get_dietary_summary_empty_event(
    organization_owner_client: Client,
    event: Event,
) -> None:
    """Test dietary summary for event with no attendees."""
    url = reverse("api:event_dietary_summary", args=[event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["restrictions"] == []
    assert data["preferences"] == []


def test_get_dietary_summary_rsvp_attendees(
    organization_owner_client: Client,
    rsvp_only_public_event: Event,
) -> None:
    """Test that RSVP attendees are included in dietary summary."""
    from accounts.models import DietaryRestriction, FoodItem

    # Create attendee with RSVP
    attendee = RevelUser.objects.create_user(username="attendee@test.com", email="attendee@test.com", password="pass")
    EventRSVP.objects.create(event=rsvp_only_public_event, user=attendee, status=EventRSVP.RsvpStatus.YES)

    # Add dietary restriction
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )

    url = reverse("api:event_dietary_summary", args=[rsvp_only_public_event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["restrictions"]) == 1
    assert data["restrictions"][0]["food_item"] == "Peanuts"
