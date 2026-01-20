"""Tests for the event pronoun distribution service."""

import pytest

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.service import event_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def attendee_with_pronouns(django_user_model: type[RevelUser]) -> RevelUser:
    """Attendee with he/him pronouns."""
    return django_user_model.objects.create_user(
        username="attendee_he@example.com",
        email="attendee_he@example.com",
        password="pass",
        pronouns="he/him",
    )


@pytest.fixture
def attendee_with_she_pronouns(django_user_model: type[RevelUser]) -> RevelUser:
    """Attendee with she/her pronouns."""
    return django_user_model.objects.create_user(
        username="attendee_she@example.com",
        email="attendee_she@example.com",
        password="pass",
        pronouns="she/her",
    )


@pytest.fixture
def attendee_with_they_pronouns(django_user_model: type[RevelUser]) -> RevelUser:
    """Attendee with they/them pronouns."""
    return django_user_model.objects.create_user(
        username="attendee_they@example.com",
        email="attendee_they@example.com",
        password="pass",
        pronouns="they/them",
    )


@pytest.fixture
def attendee_without_pronouns(django_user_model: type[RevelUser]) -> RevelUser:
    """Attendee without pronouns specified."""
    return django_user_model.objects.create_user(
        username="attendee_none@example.com",
        email="attendee_none@example.com",
        password="pass",
        pronouns="",
    )


@pytest.fixture
def free_tier(event: Event) -> TicketTier:
    """Free ticket tier."""
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="Free Tier",
        defaults={
            "visibility": TicketTier.Visibility.PUBLIC,
            "payment_method": TicketTier.PaymentMethod.FREE,
            "price": 0,
        },
    )
    return tier


@pytest.fixture
def online_tier(event: Event) -> TicketTier:
    """Online payment ticket tier."""
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="Online Tier",
        defaults={
            "visibility": TicketTier.Visibility.PUBLIC,
            "payment_method": TicketTier.PaymentMethod.ONLINE,
            "price": 10,
        },
    )
    return tier


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    """Offline payment ticket tier."""
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="Offline Tier",
        defaults={
            "visibility": TicketTier.Visibility.PUBLIC,
            "payment_method": TicketTier.PaymentMethod.OFFLINE,
            "price": 10,
        },
    )
    return tier


@pytest.fixture
def at_the_door_tier(event: Event) -> TicketTier:
    """At the door payment ticket tier."""
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="At The Door Tier",
        defaults={
            "visibility": TicketTier.Visibility.PUBLIC,
            "payment_method": TicketTier.PaymentMethod.AT_THE_DOOR,
            "price": 15,
        },
    )
    return tier


def test_empty_event_returns_zeros(event: Event) -> None:
    """Test pronoun distribution for an event with no attendees."""
    result = event_service.get_event_pronoun_distribution(event)

    assert result.distribution == []
    assert result.total_with_pronouns == 0
    assert result.total_without_pronouns == 0
    assert result.total_attendees == 0


def test_single_attendee_with_pronouns(
    event: Event,
    attendee_with_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test distribution with one attendee who has pronouns."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert len(result.distribution) == 1
    assert result.distribution[0].pronouns == "he/him"
    assert result.distribution[0].count == 1
    assert result.total_with_pronouns == 1
    assert result.total_without_pronouns == 0
    assert result.total_attendees == 1


def test_single_attendee_without_pronouns(
    event: Event,
    attendee_without_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test distribution with one attendee who has no pronouns."""
    Ticket.objects.create(
        event=event,
        user=attendee_without_pronouns,
        tier=free_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.distribution == []
    assert result.total_with_pronouns == 0
    assert result.total_without_pronouns == 1
    assert result.total_attendees == 1


def test_multiple_attendees_same_pronouns(
    event: Event,
    attendee_with_pronouns: RevelUser,
    django_user_model: type[RevelUser],
    free_tier: TicketTier,
) -> None:
    """Test that attendees with same pronouns are aggregated."""
    attendee2 = django_user_model.objects.create_user(
        username="attendee2@example.com",
        email="attendee2@example.com",
        password="pass",
        pronouns="he/him",
    )

    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.ACTIVE,
    )
    Ticket.objects.create(
        event=event,
        user=attendee2,
        tier=free_tier,
        guest_name="Test2",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert len(result.distribution) == 1
    assert result.distribution[0].pronouns == "he/him"
    assert result.distribution[0].count == 2
    assert result.total_with_pronouns == 2
    assert result.total_attendees == 2


def test_multiple_attendees_different_pronouns(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
    attendee_with_they_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test distribution with multiple different pronouns."""
    for attendee in [attendee_with_pronouns, attendee_with_she_pronouns, attendee_with_they_pronouns]:
        Ticket.objects.create(
            event=event,
            user=attendee,
            tier=free_tier,
            guest_name="Test",
            status=Ticket.TicketStatus.ACTIVE,
        )

    result = event_service.get_event_pronoun_distribution(event)

    assert len(result.distribution) == 3
    pronouns_set = {p.pronouns for p in result.distribution}
    assert pronouns_set == {"he/him", "she/her", "they/them"}
    assert result.total_with_pronouns == 3
    assert result.total_without_pronouns == 0
    assert result.total_attendees == 3


def test_mixed_with_and_without_pronouns(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_without_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test distribution with mix of attendees with and without pronouns."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.ACTIVE,
    )
    Ticket.objects.create(
        event=event,
        user=attendee_without_pronouns,
        tier=free_tier,
        guest_name="Test2",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert len(result.distribution) == 1
    assert result.distribution[0].pronouns == "he/him"
    assert result.total_with_pronouns == 1
    assert result.total_without_pronouns == 1
    assert result.total_attendees == 2


def test_rsvp_attendees_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
) -> None:
    """Test that RSVP YES attendees are included."""
    event.requires_ticket = False
    event.save()

    EventRSVP.objects.create(
        event=event,
        user=attendee_with_pronouns,
        status=EventRSVP.RsvpStatus.YES,
    )
    EventRSVP.objects.create(
        event=event,
        user=attendee_with_she_pronouns,
        status=EventRSVP.RsvpStatus.YES,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 2
    assert result.total_with_pronouns == 2


def test_rsvp_maybe_and_no_not_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
    attendee_with_they_pronouns: RevelUser,
) -> None:
    """Test that RSVP MAYBE and NO are not included."""
    event.requires_ticket = False
    event.save()

    EventRSVP.objects.create(
        event=event,
        user=attendee_with_pronouns,
        status=EventRSVP.RsvpStatus.YES,
    )
    EventRSVP.objects.create(
        event=event,
        user=attendee_with_she_pronouns,
        status=EventRSVP.RsvpStatus.MAYBE,
    )
    EventRSVP.objects.create(
        event=event,
        user=attendee_with_they_pronouns,
        status=EventRSVP.RsvpStatus.NO,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 1
    assert result.distribution[0].pronouns == "he/him"


def test_active_online_ticket_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    online_tier: TicketTier,
) -> None:
    """Test that active online payment tickets are included."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=online_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 1


def test_pending_online_ticket_not_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    online_tier: TicketTier,
) -> None:
    """Test that pending online payment tickets are not included."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=online_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.PENDING,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 0


def test_offline_ticket_any_status_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
    offline_tier: TicketTier,
) -> None:
    """Test that offline tickets count regardless of status (except cancelled)."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=offline_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.ACTIVE,
    )
    Ticket.objects.create(
        event=event,
        user=attendee_with_she_pronouns,
        tier=offline_tier,
        guest_name="Test2",
        status=Ticket.TicketStatus.PENDING,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 2


def test_at_the_door_ticket_any_status_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
    at_the_door_tier: TicketTier,
) -> None:
    """Test that at_the_door tickets count regardless of status (except cancelled)."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=at_the_door_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.ACTIVE,
    )
    Ticket.objects.create(
        event=event,
        user=attendee_with_she_pronouns,
        tier=at_the_door_tier,
        guest_name="Test2",
        status=Ticket.TicketStatus.PENDING,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 2


def test_free_ticket_any_status_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    attendee_with_she_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test that free tickets count regardless of status (except cancelled)."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.ACTIVE,
    )
    Ticket.objects.create(
        event=event,
        user=attendee_with_she_pronouns,
        tier=free_tier,
        guest_name="Test2",
        status=Ticket.TicketStatus.PENDING,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 2


def test_cancelled_ticket_not_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    free_tier: TicketTier,
    offline_tier: TicketTier,
) -> None:
    """Test that cancelled tickets are not included regardless of payment method."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.CANCELLED,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 0


def test_cancelled_offline_ticket_not_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    offline_tier: TicketTier,
) -> None:
    """Test that cancelled offline tickets are not included."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=offline_tier,
        guest_name="Test1",
        status=Ticket.TicketStatus.CANCELLED,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 0


def test_checked_in_ticket_included(
    event: Event,
    attendee_with_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test that checked-in tickets are included."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.CHECKED_IN,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 1


def test_user_with_both_ticket_and_rsvp_counted_once(
    event: Event,
    attendee_with_pronouns: RevelUser,
    free_tier: TicketTier,
) -> None:
    """Test that a user with both ticket and RSVP is only counted once."""
    Ticket.objects.create(
        event=event,
        user=attendee_with_pronouns,
        tier=free_tier,
        guest_name="Test",
        status=Ticket.TicketStatus.ACTIVE,
    )
    EventRSVP.objects.create(
        event=event,
        user=attendee_with_pronouns,
        status=EventRSVP.RsvpStatus.YES,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert result.total_attendees == 1
    assert result.total_with_pronouns == 1


def test_rsvp_only_event_no_tickets_exist(
    event: Event,
    django_user_model: type[RevelUser],
) -> None:
    """Test that when event has no tickets at all, only YES RSVPs are counted.

    Regression test: ~Q() with empty ticket joins was incorrectly matching all users.
    """
    event.requires_ticket = False
    event.save()

    # Create users with different RSVP statuses
    yes_user1 = django_user_model.objects.create_user(
        username="yes1@example.com", email="yes1@example.com", password="pass", pronouns="she/her"
    )
    yes_user2 = django_user_model.objects.create_user(
        username="yes2@example.com", email="yes2@example.com", password="pass", pronouns="he/him"
    )
    maybe_user = django_user_model.objects.create_user(
        username="maybe@example.com", email="maybe@example.com", password="pass", pronouns="they/them"
    )
    no_user = django_user_model.objects.create_user(
        username="no@example.com", email="no@example.com", password="pass", pronouns="she/they"
    )

    EventRSVP.objects.create(event=event, user=yes_user1, status=EventRSVP.RsvpStatus.YES)
    EventRSVP.objects.create(event=event, user=yes_user2, status=EventRSVP.RsvpStatus.YES)
    EventRSVP.objects.create(event=event, user=maybe_user, status=EventRSVP.RsvpStatus.MAYBE)
    EventRSVP.objects.create(event=event, user=no_user, status=EventRSVP.RsvpStatus.NO)

    # Verify no tickets exist
    assert Ticket.objects.filter(event=event).count() == 0

    result = event_service.get_event_pronoun_distribution(event)

    # Should only count the 2 YES RSVPs
    assert result.total_attendees == 2
    assert result.total_with_pronouns == 2
    pronouns_set = {p.pronouns for p in result.distribution}
    assert pronouns_set == {"she/her", "he/him"}


def test_distribution_ordered_by_count_descending(
    event: Event,
    django_user_model: type[RevelUser],
    free_tier: TicketTier,
) -> None:
    """Test that distribution is ordered by count descending."""
    # Create 3 she/her, 2 he/him, 1 they/them
    for i in range(3):
        user = django_user_model.objects.create_user(
            username=f"she{i}@example.com",
            email=f"she{i}@example.com",
            password="pass",
            pronouns="she/her",
        )
        Ticket.objects.create(
            event=event,
            user=user,
            tier=free_tier,
            guest_name=f"She {i}",
            status=Ticket.TicketStatus.ACTIVE,
        )

    for i in range(2):
        user = django_user_model.objects.create_user(
            username=f"he{i}@example.com",
            email=f"he{i}@example.com",
            password="pass",
            pronouns="he/him",
        )
        Ticket.objects.create(
            event=event,
            user=user,
            tier=free_tier,
            guest_name=f"He {i}",
            status=Ticket.TicketStatus.ACTIVE,
        )

    user = django_user_model.objects.create_user(
        username="they@example.com",
        email="they@example.com",
        password="pass",
        pronouns="they/them",
    )
    Ticket.objects.create(
        event=event,
        user=user,
        tier=free_tier,
        guest_name="They",
        status=Ticket.TicketStatus.ACTIVE,
    )

    result = event_service.get_event_pronoun_distribution(event)

    assert len(result.distribution) == 3
    assert result.distribution[0].pronouns == "she/her"
    assert result.distribution[0].count == 3
    assert result.distribution[1].pronouns == "he/him"
    assert result.distribution[1].count == 2
    assert result.distribution[2].pronouns == "they/them"
    assert result.distribution[2].count == 1
