"""Tests for the event dietary summary service."""

import pytest

from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, RevelUser, UserDietaryPreference
from events.models import Event, EventRSVP, OrganizationStaff, Ticket, TicketTier
from events.service import event_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def attendee_user1(django_user_model: type[RevelUser]) -> RevelUser:
    """First attendee user."""
    return django_user_model.objects.create_user(
        username="attendee1@example.com", email="attendee1@example.com", password="pass"
    )


@pytest.fixture
def attendee_user2(django_user_model: type[RevelUser]) -> RevelUser:
    """Second attendee user."""
    return django_user_model.objects.create_user(
        username="attendee2@example.com", email="attendee2@example.com", password="pass"
    )


@pytest.fixture
def attendee_user3(django_user_model: type[RevelUser]) -> RevelUser:
    """Third attendee user."""
    return django_user_model.objects.create_user(
        username="attendee3@example.com", email="attendee3@example.com", password="pass"
    )


@pytest.fixture
def general_tier(event: Event) -> TicketTier:
    """Create a general admission ticket tier.

    Uses get_or_create to handle the case where the same event object
    might be reused across test setup.
    """
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="General Admission",
        defaults={
            "visibility": TicketTier.Visibility.PUBLIC,
            "payment_method": TicketTier.PaymentMethod.FREE,
            "price": 0,
        },
    )
    return tier


def test_get_event_dietary_summary_empty(event: Event, organization_owner_user: RevelUser) -> None:
    """Test dietary summary for an event with no attendees."""
    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert summary.restrictions == []
    assert summary.preferences == []


def test_get_event_dietary_summary_single_restriction(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test dietary summary with one attendee having one restriction."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Carry EpiPen",
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].food_item == "Peanuts"
    assert summary.restrictions[0].severity == "severe_allergy"
    assert summary.restrictions[0].attendee_count == 1
    assert summary.restrictions[0].notes == ["Carry EpiPen"]


def test_get_event_dietary_summary_aggregates_same_restriction(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that multiple attendees with same restriction are aggregated."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Mild reaction",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Severe reaction",
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].food_item == "Peanuts"
    assert summary.restrictions[0].severity == "allergy"
    assert summary.restrictions[0].attendee_count == 2
    assert set(summary.restrictions[0].notes) == {"Mild reaction", "Severe reaction"}


def test_get_event_dietary_summary_different_severity_separate(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that same food with different severity levels are separate entries."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 2
    severities = {r.severity for r in summary.restrictions}
    assert severities == {"allergy", "severe_allergy"}


def test_get_event_dietary_summary_omits_empty_notes(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that empty notes are not included in the summary."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="",  # Empty
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].notes == []


def test_get_event_dietary_summary_single_preference(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test dietary summary with one attendee having one preference."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    # Use existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegetarian")
    UserDietaryPreference.objects.create(
        user=attendee_user1,
        preference=pref,
        comment="Strict vegetarian",
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.preferences) == 1
    assert summary.preferences[0].name == "Vegetarian"
    assert summary.preferences[0].attendee_count == 1
    assert summary.preferences[0].comments == ["Strict vegetarian"]


def test_get_event_dietary_summary_aggregates_preferences(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    attendee_user3: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that multiple attendees with same preference are aggregated."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user3, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    # Use existing seeded preference
    pref = DietaryPreference.objects.get(name="Vegetarian")
    UserDietaryPreference.objects.create(user=attendee_user1, preference=pref, comment="", is_public=True)
    UserDietaryPreference.objects.create(
        user=attendee_user2, preference=pref, comment="Lacto-vegetarian", is_public=True
    )
    UserDietaryPreference.objects.create(user=attendee_user3, preference=pref, comment="", is_public=True)

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.preferences) == 1
    assert summary.preferences[0].name == "Vegetarian"
    assert summary.preferences[0].attendee_count == 3
    assert summary.preferences[0].comments == ["Lacto-vegetarian"]  # Only non-empty


def test_get_event_dietary_summary_visibility_organizer_sees_all(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that organizers see both public and private dietary info."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=False,  # Private
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert - Organizer sees both
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].attendee_count == 2


def test_get_event_dietary_summary_visibility_attendee_sees_public_only(
    event: Event,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    attendee_user3: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that regular attendees only see public dietary info."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user3, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user3,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=False,  # Private
    )

    # Act - Regular attendee requests summary
    summary = event_service.get_event_dietary_summary(event, attendee_user1)

    # Assert - Only sees public restriction
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].attendee_count == 1  # Only public one


def test_get_event_dietary_summary_staff_sees_all(
    event: Event,
    organization_staff_user: RevelUser,
    staff_member: OrganizationStaff,  # fixture that makes organization_staff_user a staff member
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test that organization staff see all dietary info."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=False,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_staff_user)

    # Assert - Staff sees both
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].attendee_count == 2


def test_get_event_dietary_summary_mixed_restrictions_and_preferences(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
    attendee_user2: RevelUser,
    general_tier: TicketTier,
) -> None:
    """Test summary with both restrictions and preferences."""
    # Arrange
    Ticket.objects.create(event=event, user=attendee_user1, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee_user2, tier=general_tier, status=Ticket.TicketStatus.ACTIVE)

    # Add restrictions
    food1, _ = FoodItem.objects.get_or_create(name="Peanuts")
    food2, _ = FoodItem.objects.get_or_create(name="Shellfish")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food1,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Mild",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee_user2,
        food_item=food2,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Anaphylaxis",
        is_public=True,
    )

    # Add preferences - use existing seeded preferences
    veg_pref = DietaryPreference.objects.get(name="Vegetarian")
    vegan_pref = DietaryPreference.objects.get(name="Vegan")
    UserDietaryPreference.objects.create(user=attendee_user1, preference=veg_pref, is_public=True)
    UserDietaryPreference.objects.create(
        user=attendee_user2, preference=vegan_pref, comment="Strict vegan", is_public=True
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 2
    assert len(summary.preferences) == 2


def test_get_event_dietary_summary_rsvp_attendees(
    event: Event,
    organization_owner_user: RevelUser,
    attendee_user1: RevelUser,
) -> None:
    """Test that RSVP attendees are included in dietary summary."""
    # Arrange
    event.requires_ticket = False
    event.save()
    EventRSVP.objects.create(event=event, user=attendee_user1, status=EventRSVP.RsvpStatus.YES)

    food, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee_user1,
        food_item=food,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )

    # Act
    summary = event_service.get_event_dietary_summary(event, organization_owner_user)

    # Assert
    assert len(summary.restrictions) == 1
    assert summary.restrictions[0].food_item == "Peanuts"
