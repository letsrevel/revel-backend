"""Tests for Event visibility helpers (address and cancellation reason)."""

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    ResourceVisibility,
    Ticket,
    TicketTier,
)

# --- Tests for Event.can_user_see_address ---


class TestCanUserSeeAddress:
    """Tests for the Event.can_user_see_address() method."""

    @pytest.fixture
    def event_with_address(self, organization: Organization) -> Event:
        """An event with an address configured."""
        return Event.objects.create(
            organization=organization,
            name="Event With Address",
            start=timezone.now(),
            address="123 Test Street",
            visibility_settings={"address_visibility": ResourceVisibility.PUBLIC},
        )

    @pytest.fixture
    def ticket_tier(self, event_with_address: Event) -> TicketTier:
        """A ticket tier for the event."""
        return TicketTier.objects.create(event=event_with_address, name="General")

    # --- PUBLIC visibility tests ---

    @pytest.mark.django_db
    def test_public_visibility_anonymous_user_can_see(
        self, event_with_address: Event, django_user_model: type[RevelUser]
    ) -> None:
        """Anonymous users can see address when visibility is PUBLIC."""
        from django.contrib.auth.models import AnonymousUser

        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PUBLIC}
        event_with_address.save()

        assert event_with_address.can_user_see_address(AnonymousUser()) is True

    @pytest.mark.django_db
    def test_public_visibility_authenticated_user_can_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Authenticated users can see address when visibility is PUBLIC."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PUBLIC}
        event_with_address.save()

        assert event_with_address.can_user_see_address(nonmember_user) is True

    # --- PRIVATE visibility tests ---

    @pytest.mark.django_db
    def test_private_visibility_anonymous_user_cannot_see(self, event_with_address: Event) -> None:
        """Anonymous users cannot see address when visibility is PRIVATE."""
        from django.contrib.auth.models import AnonymousUser

        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        assert event_with_address.can_user_see_address(AnonymousUser()) is False

    @pytest.mark.django_db
    def test_private_visibility_user_without_relationship_cannot_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Users without relationship to event cannot see address when visibility is PRIVATE."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        assert event_with_address.can_user_see_address(nonmember_user) is False

    @pytest.mark.django_db
    def test_private_visibility_invited_user_can_see(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Invited users can see address when visibility is PRIVATE."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        invitation = EventInvitation.objects.create(user=nonmember_user, event=event_with_address)
        invitation.tiers.add(ticket_tier)

        assert event_with_address.can_user_see_address(nonmember_user) is True

    @pytest.mark.django_db
    def test_private_visibility_ticket_holder_can_see(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Ticket holders can see address when visibility is PRIVATE."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        Ticket.objects.create(guest_name="Test Guest", user=nonmember_user, event=event_with_address, tier=ticket_tier)

        assert event_with_address.can_user_see_address(nonmember_user) is True

    @pytest.mark.django_db
    def test_private_visibility_rsvp_yes_can_see(self, event_with_address: Event, nonmember_user: RevelUser) -> None:
        """Users with YES RSVP can see address when visibility is PRIVATE."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        EventRSVP.objects.create(user=nonmember_user, event=event_with_address, status=EventRSVP.RsvpStatus.YES)

        assert event_with_address.can_user_see_address(nonmember_user) is True

    @pytest.mark.django_db
    def test_private_visibility_rsvp_no_cannot_see(self, event_with_address: Event, nonmember_user: RevelUser) -> None:
        """Users with NO RSVP cannot see address when visibility is PRIVATE."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        EventRSVP.objects.create(user=nonmember_user, event=event_with_address, status=EventRSVP.RsvpStatus.NO)

        assert event_with_address.can_user_see_address(nonmember_user) is False

    # --- MEMBERS_ONLY visibility tests ---

    @pytest.mark.django_db
    def test_members_only_visibility_non_member_cannot_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Non-members cannot see address when visibility is MEMBERS_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.MEMBERS_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(nonmember_user) is False

    @pytest.mark.django_db
    def test_members_only_visibility_member_can_see(
        self, event_with_address: Event, member_user: RevelUser, organization_membership: OrganizationMember
    ) -> None:
        """Organization members can see address when visibility is MEMBERS_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.MEMBERS_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(member_user) is True

    # --- STAFF_ONLY visibility tests ---

    @pytest.mark.django_db
    def test_staff_only_visibility_non_staff_cannot_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Non-staff users cannot see address when visibility is STAFF_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.STAFF_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(nonmember_user) is False

    @pytest.mark.django_db
    def test_staff_only_visibility_member_cannot_see(
        self, event_with_address: Event, member_user: RevelUser, organization_membership: OrganizationMember
    ) -> None:
        """Regular members cannot see address when visibility is STAFF_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.STAFF_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(member_user) is False

    @pytest.mark.django_db
    def test_staff_only_visibility_staff_can_see(
        self, event_with_address: Event, organization_staff_user: RevelUser, staff_member: OrganizationStaff
    ) -> None:
        """Staff members can see address when visibility is STAFF_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.STAFF_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(organization_staff_user) is True

    @pytest.mark.django_db
    def test_staff_only_visibility_owner_can_see(
        self, event_with_address: Event, organization_owner_user: RevelUser
    ) -> None:
        """Organization owners can see address when visibility is STAFF_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.STAFF_ONLY}
        event_with_address.save()

        assert event_with_address.can_user_see_address(organization_owner_user) is True

    # --- ATTENDEES_ONLY visibility tests ---

    @pytest.mark.django_db
    def test_attendees_only_visibility_invited_only_cannot_see(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Users with only invitation (no ticket/RSVP) cannot see address when visibility is ATTENDEES_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event_with_address.save()

        invitation = EventInvitation.objects.create(user=nonmember_user, event=event_with_address)
        invitation.tiers.add(ticket_tier)

        assert event_with_address.can_user_see_address(nonmember_user) is False

    @pytest.mark.django_db
    def test_attendees_only_visibility_ticket_holder_can_see(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Ticket holders can see address when visibility is ATTENDEES_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event_with_address.save()

        Ticket.objects.create(guest_name="Test Guest", user=nonmember_user, event=event_with_address, tier=ticket_tier)

        assert event_with_address.can_user_see_address(nonmember_user) is True

    @pytest.mark.django_db
    def test_attendees_only_visibility_rsvp_yes_can_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Users with YES RSVP can see address when visibility is ATTENDEES_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event_with_address.save()

        EventRSVP.objects.create(user=nonmember_user, event=event_with_address, status=EventRSVP.RsvpStatus.YES)

        assert event_with_address.can_user_see_address(nonmember_user) is True

    @pytest.mark.django_db
    def test_attendees_only_visibility_rsvp_maybe_cannot_see(
        self, event_with_address: Event, nonmember_user: RevelUser
    ) -> None:
        """Users with MAYBE RSVP cannot see address when visibility is ATTENDEES_ONLY."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event_with_address.save()

        EventRSVP.objects.create(user=nonmember_user, event=event_with_address, status=EventRSVP.RsvpStatus.MAYBE)

        assert event_with_address.can_user_see_address(nonmember_user) is False

    # --- Cancelled ticket tests ---

    @pytest.mark.django_db
    def test_cancelled_ticket_does_not_grant_access(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Cancelled tickets do not grant access to address."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event_with_address.save()

        Ticket.objects.create(
            guest_name="Test Guest",
            user=nonmember_user,
            event=event_with_address,
            tier=ticket_tier,
            status=Ticket.TicketStatus.CANCELLED,
        )

        assert event_with_address.can_user_see_address(nonmember_user) is False

    @pytest.mark.django_db
    def test_pending_ticket_does_not_grant_access(
        self, event_with_address: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """Pending tickets do not count as attendance, so they do not grant address access."""
        event_with_address.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event_with_address.save()

        Ticket.objects.create(
            guest_name="Test Guest",
            user=nonmember_user,
            event=event_with_address,
            tier=ticket_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        assert event_with_address.can_user_see_address(nonmember_user) is False

    # --- Superuser/staff tests ---

    @pytest.mark.django_db
    def test_superuser_can_always_see_address(
        self, event_with_address: Event, django_user_model: type[RevelUser]
    ) -> None:
        """Superusers can always see address regardless of visibility setting."""
        superuser = django_user_model.objects.create_superuser(
            username="superuser", email="super@example.com", password="pass"
        )

        for visibility in ResourceVisibility:
            event_with_address.visibility_settings = {"address_visibility": visibility}
            event_with_address.save()
            assert event_with_address.can_user_see_address(superuser) is True, f"Failed for {visibility}"

    @pytest.mark.django_db
    def test_django_staff_can_always_see_address(
        self, event_with_address: Event, django_user_model: type[RevelUser]
    ) -> None:
        """Django staff users can always see address regardless of visibility setting."""
        staff_user = django_user_model.objects.create_user(
            username="djangostaff", email="staff@example.com", password="pass", is_staff=True
        )

        for visibility in ResourceVisibility:
            event_with_address.visibility_settings = {"address_visibility": visibility}
            event_with_address.save()
            assert event_with_address.can_user_see_address(staff_user) is True, f"Failed for {visibility}"

    # --- Staff and owner hierarchy tests ---

    @pytest.mark.django_db
    def test_owner_can_see_all_visibility_levels(
        self, event_with_address: Event, organization_owner_user: RevelUser
    ) -> None:
        """Organization owners can see address for all visibility levels."""
        for visibility in ResourceVisibility:
            event_with_address.visibility_settings = {"address_visibility": visibility}
            event_with_address.save()
            assert event_with_address.can_user_see_address(organization_owner_user) is True, f"Failed for {visibility}"

    @pytest.mark.django_db
    def test_staff_can_see_all_visibility_levels(
        self, event_with_address: Event, organization_staff_user: RevelUser, staff_member: OrganizationStaff
    ) -> None:
        """Organization staff can see address for all visibility levels."""
        for visibility in ResourceVisibility:
            event_with_address.visibility_settings = {"address_visibility": visibility}
            event_with_address.save()
            assert event_with_address.can_user_see_address(organization_staff_user) is True, f"Failed for {visibility}"


# --- Tests for Event.can_user_see_cancellation_reason ---


class TestCanUserSeeCancellationReason:
    """Tests for the Event.can_user_see_cancellation_reason() method."""

    @pytest.fixture
    def cancelled_event(self, organization: Organization) -> Event:
        """A cancelled event carrying a reason."""
        return Event.objects.create(
            organization=organization,
            name="Cancelled Event",
            start=timezone.now(),
            status=Event.EventStatus.CANCELLED,
            cancellation_reason="Venue flooded",
        )

    @pytest.fixture
    def ticket_tier(self, cancelled_event: Event) -> TicketTier:
        return TicketTier.objects.create(event=cancelled_event, name="General")

    @pytest.mark.django_db
    def test_anonymous_cannot_see(self, cancelled_event: Event) -> None:
        from django.contrib.auth.models import AnonymousUser

        assert cancelled_event.can_user_see_cancellation_reason(AnonymousUser()) is False

    @pytest.mark.django_db
    def test_unrelated_user_cannot_see(self, cancelled_event: Event, nonmember_user: RevelUser) -> None:
        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is False

    @pytest.mark.django_db
    def test_invited_only_user_cannot_see(
        self, cancelled_event: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        """A merely-invited user (no ticket/RSVP) does NOT qualify — unlike PRIVATE address."""
        invitation = EventInvitation.objects.create(user=nonmember_user, event=cancelled_event)
        invitation.tiers.add(ticket_tier)

        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is False

    @pytest.mark.django_db
    def test_ticket_holder_can_see(
        self, cancelled_event: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        Ticket.objects.create(guest_name="Guest", user=nonmember_user, event=cancelled_event, tier=ticket_tier)

        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is True

    @pytest.mark.django_db
    def test_cancelled_ticket_holder_cannot_see(
        self, cancelled_event: Event, nonmember_user: RevelUser, ticket_tier: TicketTier
    ) -> None:
        Ticket.objects.create(
            guest_name="Guest",
            user=nonmember_user,
            event=cancelled_event,
            tier=ticket_tier,
            status=Ticket.TicketStatus.CANCELLED,
        )

        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is False

    @pytest.mark.django_db
    def test_rsvp_yes_can_see(self, cancelled_event: Event, nonmember_user: RevelUser) -> None:
        EventRSVP.objects.create(user=nonmember_user, event=cancelled_event, status=EventRSVP.RsvpStatus.YES)

        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is True

    @pytest.mark.django_db
    def test_rsvp_no_cannot_see(self, cancelled_event: Event, nonmember_user: RevelUser) -> None:
        EventRSVP.objects.create(user=nonmember_user, event=cancelled_event, status=EventRSVP.RsvpStatus.NO)

        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is False

    @pytest.mark.django_db
    def test_owner_can_see(self, cancelled_event: Event, organization_owner_user: RevelUser) -> None:
        assert cancelled_event.can_user_see_cancellation_reason(organization_owner_user) is True

    @pytest.mark.django_db
    def test_staff_member_can_see(
        self, cancelled_event: Event, organization_staff_user: RevelUser, staff_member: OrganizationStaff
    ) -> None:
        assert cancelled_event.can_user_see_cancellation_reason(organization_staff_user) is True

    @pytest.mark.django_db
    def test_superuser_can_see(self, cancelled_event: Event, nonmember_user: RevelUser) -> None:
        nonmember_user.is_superuser = True
        assert cancelled_event.can_user_see_cancellation_reason(nonmember_user) is True
