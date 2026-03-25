"""Tests for notification eligibility with UNLISTED event visibility.

UNLISTED events use PRIVATE semantics for notifications: only explicitly
invited/participating users receive notifications (no org member broadcast,
no follower notifications).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.models.mixins import ResourceVisibility
from notifications.enums import NotificationType
from notifications.service.eligibility import (
    BatchParticipationChecker,
    get_eligible_users_for_event_notification,
)


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="owner", email="owner@test.com", password="pass")


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="member", email="member@test.com", password="pass")


@pytest.fixture
def invited_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="invited", email="invited@test.com", password="pass")


@pytest.fixture
def ticket_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="ticket", email="ticket@test.com", password="pass")


@pytest.fixture
def rsvp_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="rsvp", email="rsvp@test.com", password="pass")


@pytest.fixture
def random_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="random", email="random@test.com", password="pass")


@pytest.fixture
def organization(owner: RevelUser) -> Organization:
    return Organization.objects.create(name="Test Org", slug="test-org-elig", owner=owner)


@pytest.fixture
def unlisted_event(organization: Organization) -> Event:
    next_week = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Unlisted Event",
        slug="unlisted-event-elig",
        visibility=Event.Visibility.UNLISTED,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(hours=2),
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(unlisted_event: Event) -> TicketTier:
    return TicketTier.objects.get(event=unlisted_event, name="General Admission")


@pytest.fixture
def setup_participants(
    organization: Organization,
    unlisted_event: Event,
    member_user: RevelUser,
    invited_user: RevelUser,
    ticket_user: RevelUser,
    rsvp_user: RevelUser,
    ticket_tier: TicketTier,
) -> None:
    """Set up various participant types for the unlisted event."""
    OrganizationMember.objects.create(organization=organization, user=member_user)
    EventInvitation.objects.create(event=unlisted_event, user=invited_user)
    Ticket.objects.create(event=unlisted_event, user=ticket_user, tier=ticket_tier, guest_name="Ticket")
    EventRSVP.objects.create(event=unlisted_event, user=rsvp_user, status=EventRSVP.RsvpStatus.YES)


pytestmark = pytest.mark.django_db


class TestUnlistedEventNotificationEligibility:
    """UNLISTED events use PRIVATE semantics: only explicit participants get notifications."""

    def test_owner_is_eligible(self, unlisted_event: Event, owner: RevelUser) -> None:
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert owner.id in {u.id for u in eligible}

    def test_invited_user_is_eligible(
        self, unlisted_event: Event, invited_user: RevelUser, setup_participants: None
    ) -> None:
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert invited_user.id in {u.id for u in eligible}

    def test_ticket_holder_is_eligible(
        self, unlisted_event: Event, ticket_user: RevelUser, setup_participants: None
    ) -> None:
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert ticket_user.id in {u.id for u in eligible}

    def test_rsvp_user_is_eligible(self, unlisted_event: Event, rsvp_user: RevelUser, setup_participants: None) -> None:
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert rsvp_user.id in {u.id for u in eligible}

    def test_org_member_without_participation_not_eligible(
        self, unlisted_event: Event, member_user: RevelUser, setup_participants: None
    ) -> None:
        """Org members are NOT notified for UNLISTED events (PRIVATE semantics)."""
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert member_user.id not in {u.id for u in eligible}

    def test_org_member_not_eligible_for_event_open(
        self, unlisted_event: Event, member_user: RevelUser, setup_participants: None
    ) -> None:
        """EVENT_OPEN on UNLISTED events does NOT broadcast to org members."""
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_OPEN))
        assert member_user.id not in {u.id for u in eligible}

    def test_random_user_not_eligible(
        self, unlisted_event: Event, random_user: RevelUser, setup_participants: None
    ) -> None:
        eligible = list(get_eligible_users_for_event_notification(unlisted_event, NotificationType.EVENT_UPDATED))
        assert random_user.id not in {u.id for u in eligible}


class TestUnlistedAddressVisibilityBatchParity:
    """BatchParticipationChecker.can_see_address handles UNLISTED like PUBLIC."""

    def test_unlisted_address_visible_to_random_user(self, unlisted_event: Event, random_user: RevelUser) -> None:
        unlisted_event.address_visibility = ResourceVisibility.UNLISTED
        unlisted_event.save()

        checker = BatchParticipationChecker(unlisted_event)
        assert checker.can_see_address(random_user.id) is True
        assert unlisted_event.can_user_see_address(random_user) is True
