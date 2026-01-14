"""Tests for waitlist notification signals related to RSVPs and edge cases.

Split from test_waitlist_signals.py for maintainability.
Ticket-related tests remain in test_waitlist_signals.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventRSVP, EventWaitList, Ticket, TicketTier
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db(transaction=True)


# Fixtures online_tier, offline_tier, at_the_door_tier, free_tier are in test_waitlist_signals.py
# They use public_event from conftest.py


@pytest.fixture
def online_tier(public_event: Event) -> TicketTier:
    """A ticket tier with online payment."""
    public_event.max_attendees = 2
    public_event.waitlist_open = True
    public_event.save()

    return TicketTier.objects.create(
        event=public_event,
        name="Online Tier",
        price=10.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


class TestRSVPWaitlistRemoval:
    """Test automatic removal from waitlist when RSVPs are made."""

    def test_rsvp_yes_removes_from_waitlist(
        self,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test that RSVP YES removes user from waitlist.

        When a user RSVPs YES to an event, they should be removed from
        the waitlist as they have secured a spot.
        """
        # Arrange
        public_event.requires_ticket = False
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - RSVP YES
        EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_rsvp_no_removes_from_waitlist(
        self,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test that RSVP NO removes user from waitlist.

        When a user RSVPs NO to an event, they should be removed from
        the waitlist as they've indicated they won't attend.
        """
        # Arrange
        public_event.requires_ticket = False
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - RSVP NO
        EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.NO,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_rsvp_maybe_does_not_remove_from_waitlist(
        self,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test that RSVP MAYBE keeps user on waitlist.

        When a user RSVPs MAYBE to an event, they should remain on the
        waitlist as they haven't committed to attending.
        """
        # Arrange
        public_event.requires_ticket = False
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - RSVP MAYBE
        EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.MAYBE,
        )

        # Assert - User still on waitlist
        assert EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_rsvp_change_from_maybe_to_yes_removes_from_waitlist(
        self,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test that changing RSVP from MAYBE to YES removes from waitlist.

        When a user changes their RSVP from MAYBE to YES, they should be
        removed from the waitlist.
        """
        # Arrange
        public_event.requires_ticket = False
        public_event.save()
        EventWaitList.objects.create(event=public_event, user=member_user)

        rsvp = EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.MAYBE,
        )

        # Verify user still on waitlist
        assert EventWaitList.objects.filter(event=public_event, user=member_user).exists()

        # Act - Change to YES
        rsvp.status = EventRSVP.RsvpStatus.YES
        rsvp.save()

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()


class TestRSVPCancellationNotifications:
    """Test waitlist notifications when RSVPs are changed/cancelled."""

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_rsvp_yes_to_no_notifies_waitlist_when_full(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that changing RSVP from YES to NO notifies waitlist when event was full.

        When an event is at capacity and someone changes their RSVP from YES to NO,
        users on the waitlist should be notified that a spot became available.
        """
        # Arrange - Event without tickets, capacity of 2
        public_event.requires_ticket = False
        public_event.max_attendees = 2
        public_event.waitlist_open = True
        public_event.save()

        # Fill event to capacity with YES RSVPs
        rsvp1 = EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.YES,
        )
        EventRSVP.objects.create(
            event=public_event,
            user=nonmember_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Add user to waitlist
        waitlist_user = RevelUser.objects.create_user(
            username="waitlist@example.com",
            email="waitlist@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user)

        # Act - Change RSVP from YES to NO
        rsvp1.status = EventRSVP.RsvpStatus.NO
        rsvp1.save()

        # Assert - Waitlist was notified
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 1
        call_kwargs = waitlist_calls[0][1]
        assert call_kwargs["user"] == waitlist_user
        assert call_kwargs["notification_type"] == NotificationType.WAITLIST_SPOT_AVAILABLE
        assert call_kwargs["context"]["event_id"] == str(public_event.id)
        assert call_kwargs["context"]["spots_available"] == 1

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_rsvp_yes_to_maybe_notifies_waitlist_when_full(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that changing RSVP from YES to MAYBE notifies waitlist when event was full.

        When an event is at capacity and someone changes their RSVP from YES to MAYBE,
        users on the waitlist should be notified that a spot became available.
        """
        # Arrange - Event without tickets, capacity of 2
        public_event.requires_ticket = False
        public_event.max_attendees = 2
        public_event.waitlist_open = True
        public_event.save()

        # Fill event to capacity
        rsvp1 = EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.YES,
        )
        EventRSVP.objects.create(
            event=public_event,
            user=nonmember_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Add user to waitlist
        waitlist_user = RevelUser.objects.create_user(
            username="waitlist@example.com",
            email="waitlist@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user)

        # Act - Change RSVP from YES to MAYBE
        rsvp1.status = EventRSVP.RsvpStatus.MAYBE
        rsvp1.save()

        # Assert - Waitlist was notified
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 1
        call_kwargs = waitlist_calls[0][1]
        assert call_kwargs["notification_type"] == NotificationType.WAITLIST_SPOT_AVAILABLE

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_rsvp_deletion_notifies_waitlist(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that deleting an RSVP notifies waitlist when event was full.

        When an RSVP is deleted and the event was at capacity, waitlist
        users should be notified.
        """
        # Arrange - Event without tickets, capacity of 2
        public_event.requires_ticket = False
        public_event.max_attendees = 2
        public_event.waitlist_open = True
        public_event.save()

        # Fill event to capacity
        rsvp1 = EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.YES,
        )
        EventRSVP.objects.create(
            event=public_event,
            user=nonmember_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Add user to waitlist
        waitlist_user = RevelUser.objects.create_user(
            username="waitlist@example.com",
            email="waitlist@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user)

        # Act - Delete RSVP
        rsvp1.delete()

        # Assert - Waitlist was notified
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 1
        call_kwargs = waitlist_calls[0][1]
        assert call_kwargs["notification_type"] == NotificationType.WAITLIST_SPOT_AVAILABLE


# ===== Edge Cases and Additional Scenarios =====


class TestWaitlistNotificationEdgeCases:
    """Test edge cases for waitlist notifications."""

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_no_notification_when_waitlist_not_open(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that notifications are not sent when waitlist is not open.

        Even if spots become available, waitlist should not be notified if
        the waitlist_open flag is False.
        """
        # Arrange - Close waitlist
        public_event.waitlist_open = False
        public_event.save()

        # Fill event
        ticket1 = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=nonmember_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Add user to waitlist
        waitlist_user = RevelUser.objects.create_user(
            username="waitlist@example.com",
            email="waitlist@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user)

        # Act - Cancel ticket
        ticket1.status = Ticket.TicketStatus.CANCELLED
        ticket1.save()

        # Assert - No waitlist notification sent (only TICKET_CANCELLED)
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 0

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_no_notification_when_no_max_attendees(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        member_user: RevelUser,
    ) -> None:
        """Test that notifications are not sent for unlimited capacity events.

        Events with max_attendees=0 (unlimited capacity) should never trigger
        waitlist notifications.
        """
        # Arrange - Event with unlimited capacity
        public_event.max_attendees = 0
        public_event.waitlist_open = True
        public_event.requires_ticket = False
        public_event.save()

        # Add user to waitlist
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create and delete RSVP
        rsvp = EventRSVP.objects.create(
            event=public_event,
            user=member_user,
            status=EventRSVP.RsvpStatus.YES,
        )
        rsvp.delete()

        # Assert - No waitlist notification sent
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 0

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_multiple_waitlist_users_all_notified(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that all users on waitlist are notified when spots become available.

        When a spot becomes available, all waitlisted users should receive
        a notification (not just the first one).
        """
        # Arrange - Fill event
        ticket1 = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=nonmember_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Add multiple users to waitlist
        waitlist_user1 = RevelUser.objects.create_user(
            username="waitlist1@example.com",
            email="waitlist1@example.com",
            password="pass",
        )
        waitlist_user2 = RevelUser.objects.create_user(
            username="waitlist2@example.com",
            email="waitlist2@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user1)
        EventWaitList.objects.create(event=public_event, user=waitlist_user2)

        # Act - Cancel ticket
        ticket1.status = Ticket.TicketStatus.CANCELLED
        ticket1.save()

        # Assert - Both users notified with waitlist notifications
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only (ignore TICKET_CANCELLED)
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 2

        # Verify both users received notification
        notified_users = [call[1]["user"] for call in waitlist_calls]
        assert waitlist_user1 in notified_users
        assert waitlist_user2 in notified_users

    def test_notification_context_contains_required_fields(
        self,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that waitlist notification context includes all required fields.

        The notification context should include event details, organization info,
        and the number of spots available.
        """
        # Arrange - Fill event
        ticket1 = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=nonmember_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Add user to waitlist
        waitlist_user = RevelUser.objects.create_user(
            username="waitlist@example.com",
            email="waitlist@example.com",
            password="pass",
        )
        EventWaitList.objects.create(event=public_event, user=waitlist_user)

        with patch("notifications.signals.waitlist.notification_requested.send") as mock_signal:
            # Act - Cancel ticket
            ticket1.status = Ticket.TicketStatus.CANCELLED
            ticket1.save()

            # Assert - Context has required fields
            # Filter for WAITLIST_SPOT_AVAILABLE notifications only
            waitlist_calls = [
                call
                for call in mock_signal.call_args_list
                if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
            ]
            assert len(waitlist_calls) == 1

            call_kwargs = waitlist_calls[0][1]
            context = call_kwargs["context"]

            assert "event_id" in context
            assert "event_name" in context
            assert "event_start" in context
            assert "event_start_formatted" in context
            assert "event_location" in context
            assert "event_url" in context
            assert "organization_id" in context
            assert "organization_name" in context
            assert "spots_available" in context

            assert context["event_id"] == str(public_event.id)
            assert context["event_name"] == public_event.name
            assert context["organization_id"] == str(public_event.organization_id)
            assert context["spots_available"] == 1
