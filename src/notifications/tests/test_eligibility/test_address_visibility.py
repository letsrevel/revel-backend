"""Tests for notification eligibility: batch address visibility parity.

The fixtures come from this package's ``conftest.py``.
"""

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
    TicketTier,
)
from events.models.mixins import ResourceVisibility
from notifications.service.eligibility import (
    BatchParticipationChecker,
)


@pytest.mark.django_db
class TestBatchAddressVisibilityParity:
    """Tests verifying BatchParticipationChecker.can_see_address matches Event.can_user_see_address."""

    def test_public_visibility_everyone_can_see(
        self,
        event: Event,
        random_user: RevelUser,
    ) -> None:
        """PUBLIC visibility: everyone can see address."""
        event.visibility_settings = {"address_visibility": ResourceVisibility.PUBLIC}
        event.save()

        checker = BatchParticipationChecker(event)

        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is True

    def test_staff_only_visibility_staff_can_see(
        self,
        event: Event,
        organization: Organization,
        owner: RevelUser,
        staff_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """STAFF_ONLY visibility: only staff/owners can see address."""
        OrganizationStaff.objects.create(organization=organization, user=staff_user)
        event.visibility_settings = {"address_visibility": ResourceVisibility.STAFF_ONLY}
        event.save()

        checker = BatchParticipationChecker(event)

        # Owner can see
        assert checker.can_see_address(owner.id) == event.can_user_see_address(owner)
        assert checker.can_see_address(owner.id) is True

        # Staff can see
        assert checker.can_see_address(staff_user.id) == event.can_user_see_address(staff_user)
        assert checker.can_see_address(staff_user.id) is True

        # Random user cannot see
        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is False

    def test_members_only_visibility_members_can_see(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """MEMBERS_ONLY visibility: organization members can see address."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        event.visibility_settings = {"address_visibility": ResourceVisibility.MEMBERS_ONLY}
        event.save()

        checker = BatchParticipationChecker(event)

        # Member can see
        assert checker.can_see_address(member_user.id) == event.can_user_see_address(member_user)
        assert checker.can_see_address(member_user.id) is True

        # Random user cannot see
        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is False

    def test_attendees_only_visibility_ticket_holders_can_see(
        self,
        event: Event,
        ticket_user: RevelUser,
        ticket_tier: TicketTier,
        random_user: RevelUser,
    ) -> None:
        """ATTENDEES_ONLY visibility: ticket holders can see address."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Test",
        )
        event.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event.save()

        checker = BatchParticipationChecker(event)

        # Ticket holder can see
        assert checker.can_see_address(ticket_user.id) == event.can_user_see_address(ticket_user)
        assert checker.can_see_address(ticket_user.id) is True

        # Random user cannot see
        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is False

    def test_attendees_only_visibility_maybe_rsvp_cannot_see(
        self,
        event: Event,
        rsvp_user: RevelUser,
    ) -> None:
        """ATTENDEES_ONLY visibility: a MAYBE RSVP is not confirmed attendance."""
        EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.MAYBE)
        event.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event.save()

        checker = BatchParticipationChecker(event)

        assert checker.can_see_address(rsvp_user.id) == event.can_user_see_address(rsvp_user)
        assert checker.can_see_address(rsvp_user.id) is False

    def test_attendees_only_visibility_rsvp_users_can_see(
        self,
        event: Event,
        rsvp_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """ATTENDEES_ONLY visibility: RSVP'd users can see address."""
        EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.YES)
        event.visibility_settings = {"address_visibility": ResourceVisibility.ATTENDEES_ONLY}
        event.save()

        checker = BatchParticipationChecker(event)

        # RSVP user can see
        assert checker.can_see_address(rsvp_user.id) == event.can_user_see_address(rsvp_user)
        assert checker.can_see_address(rsvp_user.id) is True

    def test_private_visibility_invited_users_can_see(
        self,
        event: Event,
        invited_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """PRIVATE visibility: invited users can see address."""
        EventInvitation.objects.create(event=event, user=invited_user)
        event.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event.save()

        checker = BatchParticipationChecker(event)

        # Invited user can see
        assert checker.can_see_address(invited_user.id) == event.can_user_see_address(invited_user)
        assert checker.can_see_address(invited_user.id) is True

        # Random user cannot see
        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is False

    def test_private_visibility_ticket_holders_can_see(
        self,
        event: Event,
        ticket_user: RevelUser,
        ticket_tier: TicketTier,
    ) -> None:
        """PRIVATE visibility: ticket holders can see address."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Test",
        )
        event.visibility_settings = {"address_visibility": ResourceVisibility.PRIVATE}
        event.save()

        checker = BatchParticipationChecker(event)

        # Ticket holder can see
        assert checker.can_see_address(ticket_user.id) == event.can_user_see_address(ticket_user)
        assert checker.can_see_address(ticket_user.id) is True

    def test_staff_can_always_see_non_staff_only_visibility(
        self,
        event: Event,
        organization: Organization,
        owner: RevelUser,
    ) -> None:
        """Staff/owners can see address for all visibility levels except explicitly checking STAFF_ONLY logic."""
        for visibility in [
            ResourceVisibility.PRIVATE,
            ResourceVisibility.MEMBERS_ONLY,
            ResourceVisibility.ATTENDEES_ONLY,
        ]:
            event.visibility_settings = {"address_visibility": visibility}
            event.save()

            checker = BatchParticipationChecker(event)

            # Owner can always see
            assert checker.can_see_address(owner.id) == event.can_user_see_address(owner)
            assert checker.can_see_address(owner.id) is True

    def test_empty_blob_falls_back_to_public_default(
        self,
        event: Event,
        random_user: RevelUser,
    ) -> None:
        """No ``address_visibility`` key at all: both implementations must fall back the same way."""
        event.visibility_settings = {}
        event.save()

        checker = BatchParticipationChecker(event)

        assert checker.can_see_address(random_user.id) == event.can_user_see_address(random_user)
        assert checker.can_see_address(random_user.id) is True
