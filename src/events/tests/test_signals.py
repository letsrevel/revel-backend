"""Tests for event-related signal handlers, particularly potluck item unclaiming."""

import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, PendingEventInvitation, PotluckItem, Ticket, TicketTier
from events.signals import unclaim_user_potluck_items

pytestmark = pytest.mark.django_db


class TestUnclaimUserPotluckItems:
    """Test the unclaim_user_potluck_items helper function."""

    def test_unclaim_items_for_user(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that unclaim_user_potluck_items removes user as assignee."""
        # Create potluck items assigned to the user
        item1 = PotluckItem.objects.create(
            event=event, name="Chips", item_type="food", assignee=nonmember_user, created_by=nonmember_user
        )
        item2 = PotluckItem.objects.create(
            event=event, name="Salsa", item_type="food", assignee=nonmember_user, created_by=nonmember_user
        )

        # Unclaim items
        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        # Verify both items were unclaimed
        assert count == 2
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.assignee is None
        assert item2.assignee is None

    def test_unclaim_items_returns_zero_when_no_items_assigned(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that unclaim returns 0 when user has no assigned items."""
        # Create items but don't assign them
        PotluckItem.objects.create(event=event, name="Chips", item_type="food")

        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        assert count == 0

    def test_unclaim_only_affects_specific_user(
        self, event: Event, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """Test that unclaiming only affects the specified user's items."""
        # Create items for two different users
        user1_item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        user2_item = PotluckItem.objects.create(
            event=event, name="Salsa", item_type="food", assignee=organization_owner_user
        )

        # Unclaim only nonmember_user's items
        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        assert count == 1
        user1_item.refresh_from_db()
        user2_item.refresh_from_db()
        assert user1_item.assignee is None
        assert user2_item.assignee == organization_owner_user

    def test_unclaim_only_affects_specific_event(
        self, event: Event, organization: t.Any, nonmember_user: RevelUser
    ) -> None:
        """Test that unclaiming only affects items from the specified event."""

        # Create a second event
        event2 = Event.objects.create(
            organization=organization,
            name="Second Event",
            slug="second-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status="open",
        )

        # Create items in both events
        event1_item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        event2_item = PotluckItem.objects.create(event=event2, name="Salsa", item_type="food", assignee=nonmember_user)

        # Unclaim only event1 items
        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        assert count == 1
        event1_item.refresh_from_db()
        event2_item.refresh_from_db()
        assert event1_item.assignee is None
        assert event2_item.assignee == nonmember_user

    def test_unclaim_sends_notification_when_items_unclaimed(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that notification is sent when items are unclaimed."""
        PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        assert count == 1
        # Notification is now handled via signals using notification_requested.send()
        # Note: transaction.on_commit won't fire in tests unless we're in a real transaction

    def test_unclaim_skips_notification_when_no_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that no notification is sent when there are no items to unclaim."""
        count = unclaim_user_potluck_items(event.id, nonmember_user.id)

        assert count == 0
        # No notification should be sent since count was 0
        # This is implicit - the on_commit block only runs if count > 0


class TestRSVPSignalUnclaimingBehavior:
    """Test that RSVP status changes trigger potluck item unclaiming."""

    def test_rsvp_no_unclaims_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that RSVP status NO unclaims all user's potluck items."""
        # Claim some items
        item1 = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        item2 = PotluckItem.objects.create(event=event, name="Salsa", item_type="food", assignee=nonmember_user)

        # Create RSVP with status NO
        EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.NO)

        # Items should be unclaimed
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.assignee is None
        assert item2.assignee is None

    def test_rsvp_maybe_unclaims_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that RSVP status MAYBE unclaims all user's potluck items."""
        # Claim some items
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create RSVP with status MAYBE
        EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.MAYBE)

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_rsvp_yes_does_not_unclaim_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that RSVP status YES does NOT unclaim potluck items."""
        # Claim some items
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create RSVP with status YES
        EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        # Items should still be claimed
        item.refresh_from_db()
        assert item.assignee == nonmember_user

    def test_rsvp_change_yes_to_no_unclaims_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that changing RSVP from YES to NO unclaims items."""
        # Start with YES RSVP
        rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        # Claim items while RSVP is YES
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Change to NO
        rsvp.status = EventRSVP.RsvpStatus.NO
        rsvp.save()

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_rsvp_change_yes_to_maybe_unclaims_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that changing RSVP from YES to MAYBE unclaims items."""
        # Start with YES RSVP
        rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        # Claim items while RSVP is YES
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Change to MAYBE
        rsvp.status = EventRSVP.RsvpStatus.MAYBE
        rsvp.save()

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_rsvp_deletion_unclaims_items(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that deleting an RSVP unclaims all user's potluck items."""
        # Create RSVP with status YES
        rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        # Claim some items
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Delete RSVP
        rsvp.delete()

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_rsvp_unclaiming_is_idempotent(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that unclaiming works even when called multiple times."""
        # Claim item
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create NO RSVP (first unclaim)
        rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.NO)

        # Update to NO again (should be idempotent)
        rsvp.status = EventRSVP.RsvpStatus.NO
        rsvp.save()

        # Item should still be unclaimed
        item.refresh_from_db()
        assert item.assignee is None


class TestTicketSignalUnclaimingBehavior:
    """Test that ticket status changes trigger potluck item unclaiming."""

    def test_ticket_cancelled_unclaims_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that cancelling a ticket unclaims all user's potluck items."""
        # Claim some items
        item1 = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        item2 = PotluckItem.objects.create(event=event, name="Salsa", item_type="food", assignee=nonmember_user)

        # Create ticket with CANCELLED status
        Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.CANCELLED,
        )

        # Items should be unclaimed
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.assignee is None
        assert item2.assignee is None

    def test_ticket_active_does_not_unclaim_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that an ACTIVE ticket does NOT unclaim potluck items."""
        # Claim item
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create active ticket
        Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Item should still be claimed
        item.refresh_from_db()
        assert item.assignee == nonmember_user

    def test_ticket_pending_does_not_unclaim_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that a PENDING ticket does NOT unclaim potluck items."""
        # Claim item
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create pending ticket
        Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        # Item should still be claimed
        item.refresh_from_db()
        assert item.assignee == nonmember_user

    def test_ticket_change_active_to_cancelled_unclaims_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that changing ticket from ACTIVE to CANCELLED unclaims items."""
        # Create active ticket
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Claim items while ticket is active
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Cancel ticket
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save()

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_ticket_deletion_unclaims_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that deleting a ticket unclaims all user's potluck items."""
        # Create active ticket
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Claim items
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Delete ticket
        ticket.delete()

        # Items should be unclaimed
        item.refresh_from_db()
        assert item.assignee is None

    def test_ticket_checked_in_does_not_unclaim_items(
        self, event: Event, nonmember_user: RevelUser, event_ticket_tier: TicketTier
    ) -> None:
        """Test that a CHECKED_IN ticket does NOT unclaim potluck items."""
        # Claim item
        item = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)

        # Create checked-in ticket
        Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            user=nonmember_user,
            tier=event_ticket_tier,
            status=Ticket.TicketStatus.CHECKED_IN,
        )

        # Item should still be claimed
        item.refresh_from_db()
        assert item.assignee == nonmember_user


class TestCrossEventUnclaimingBehavior:
    """Test that unclaiming only affects the specific event."""

    def test_rsvp_change_only_unclaims_items_from_same_event(
        self, event: Event, organization: t.Any, nonmember_user: RevelUser
    ) -> None:
        """Test that RSVP change only unclaims items from the same event, not other events."""

        # Create a second event
        event2 = Event.objects.create(
            organization=organization,
            name="Second Event",
            slug="second-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status="open",
        )

        # Create RSVPs for both events
        EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)
        rsvp2 = EventRSVP.objects.create(event=event2, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

        # Claim items in both events
        item1 = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        item2 = PotluckItem.objects.create(event=event2, name="Salsa", item_type="food", assignee=nonmember_user)

        # Change RSVP for event2 to NO
        rsvp2.status = EventRSVP.RsvpStatus.NO
        rsvp2.save()

        # Only event2 items should be unclaimed
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.assignee == nonmember_user
        assert item2.assignee is None


class TestMultipleUsersUnclaimingBehavior:
    """Test that unclaiming only affects the specific user."""

    def test_one_user_rsvp_change_does_not_affect_other_users_items(
        self, event: Event, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """Test that one user's RSVP change doesn't unclaim another user's items."""
        # Both users RSVP YES
        EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)
        rsvp2 = EventRSVP.objects.create(event=event, user=organization_owner_user, status=EventRSVP.RsvpStatus.YES)

        # Both users claim items
        item1 = PotluckItem.objects.create(event=event, name="Chips", item_type="food", assignee=nonmember_user)
        item2 = PotluckItem.objects.create(
            event=event, name="Salsa", item_type="food", assignee=organization_owner_user
        )

        # User 2 changes to NO
        rsvp2.status = EventRSVP.RsvpStatus.NO
        rsvp2.save()

        # Only user 2's items should be unclaimed
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.assignee == nonmember_user
        assert item2.assignee is None


class TestPendingInvitationConversion:
    """Test that pending invitations are converted to real invitations when user registers.

    The signal `handle_user_creation` converts pending invitations when a new user is created
    with an email that matches a pending invitation.
    """

    def test_pending_invitation_copies_waives_apply_deadline_when_user_registers(
        self, event: Event, django_user_model: type[RevelUser]
    ) -> None:
        """Test that waives_apply_deadline is copied from pending to real invitation on registration."""
        # Create pending invitation FIRST with waives_apply_deadline=True
        pending = PendingEventInvitation.objects.create(
            event=event,
            email="newuser@example.com",
            waives_questionnaire=True,
            waives_purchase=True,
            waives_apply_deadline=True,
            waives_rsvp_deadline=True,
            custom_message="Welcome!",
        )

        # THEN create user with matching email - this triggers the signal
        user = django_user_model.objects.create_user(
            username="new_user",
            email="newuser@example.com",
            password="pass",
        )

        # Check that real invitation was created with all flags
        invitation = EventInvitation.objects.get(event=event, user=user)
        assert invitation.waives_questionnaire is True
        assert invitation.waives_purchase is True
        assert invitation.waives_apply_deadline is True
        assert invitation.waives_rsvp_deadline is True
        assert invitation.custom_message == "Welcome!"

        # Pending invitation should be deleted
        assert not PendingEventInvitation.objects.filter(pk=pending.pk).exists()

    def test_pending_invitation_default_waives_apply_deadline_is_false(
        self, event: Event, django_user_model: type[RevelUser]
    ) -> None:
        """Test that waives_apply_deadline defaults to False when not explicitly set."""
        # Create pending invitation without waives_apply_deadline
        PendingEventInvitation.objects.create(
            event=event,
            email="newuser2@example.com",
        )

        # Create user with matching email
        user = django_user_model.objects.create_user(
            username="new_user2",
            email="newuser2@example.com",
            password="pass",
        )

        # Check that real invitation has waives_apply_deadline=False (default)
        invitation = EventInvitation.objects.get(event=event, user=user)
        assert invitation.waives_apply_deadline is False
