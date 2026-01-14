"""Tests for waitlist notification signals."""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventWaitList, Ticket, TicketTier
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db(transaction=True)


# ===== Helper Fixtures =====


@pytest.fixture
def online_tier(public_event: Event) -> TicketTier:
    """A ticket tier with online payment."""
    # Ensure event is configured for capacity testing
    public_event.max_attendees = 2
    public_event.waitlist_open = True
    public_event.save()

    return TicketTier.objects.create(
        event=public_event,
        name="Online Tier",
        price=10.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def offline_tier(public_event: Event) -> TicketTier:
    """A ticket tier with offline payment."""
    # Ensure event is configured for capacity testing
    public_event.max_attendees = 2
    public_event.waitlist_open = True
    public_event.save()

    return TicketTier.objects.create(
        event=public_event,
        name="Offline Tier",
        price=10.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def at_the_door_tier(public_event: Event) -> TicketTier:
    """A ticket tier with at-the-door payment."""
    # Ensure event is configured for capacity testing
    public_event.max_attendees = 2
    public_event.waitlist_open = True
    public_event.save()

    return TicketTier.objects.create(
        event=public_event,
        name="At the Door",
        price=10.00,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
    )


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    """A free ticket tier."""
    # Ensure event is configured for capacity testing
    public_event.max_attendees = 2
    public_event.waitlist_open = True
    public_event.save()

    return TicketTier.objects.create(
        event=public_event,
        name="Free Tier",
        price=0.00,
        payment_method=TicketTier.PaymentMethod.FREE,
    )


# ===== Ticket Signal Tests =====


class TestTicketWaitlistRemoval:
    """Test automatic removal from waitlist when tickets are created/activated."""

    def test_online_payment_ticket_pending_does_not_remove_from_waitlist(
        self,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that PENDING tickets with online payment don't remove from waitlist.

        For online payment tickets, users should remain on the waitlist until
        payment is completed and ticket becomes ACTIVE.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create PENDING ticket with online payment
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        # Assert - User still on waitlist
        assert EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_online_payment_ticket_active_removes_from_waitlist(
        self,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that ACTIVE tickets with online payment remove from waitlist.

        When an online payment is completed and ticket becomes ACTIVE,
        the user should be removed from the waitlist.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create ACTIVE ticket (payment completed)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_offline_payment_ticket_pending_removes_from_waitlist(
        self,
        public_event: Event,
        offline_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that PENDING tickets with offline payment remove from waitlist.

        For offline payment, the spot is reserved immediately when ticket is created
        as PENDING, so user should be removed from waitlist.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create PENDING ticket with offline payment
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=offline_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_at_the_door_payment_ticket_pending_removes_from_waitlist(
        self,
        public_event: Event,
        at_the_door_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that PENDING tickets with at-the-door payment remove from waitlist.

        For at-the-door payment, the spot is reserved immediately when ticket is created
        as PENDING, so user should be removed from waitlist.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create PENDING ticket with at-the-door payment
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=at_the_door_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_free_ticket_active_removes_from_waitlist(
        self,
        public_event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that free tickets (ACTIVE) remove from waitlist.

        Free tickets are created as ACTIVE immediately and should remove
        the user from the waitlist.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)

        # Act - Create free ticket (created as ACTIVE)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_ticket_activation_removes_from_waitlist(
        self,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that activating a PENDING ticket removes from waitlist.

        When an online payment ticket transitions from PENDING to ACTIVE,
        the user should be removed from the waitlist.
        """
        # Arrange
        EventWaitList.objects.create(event=public_event, user=member_user)
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.PENDING,
        )

        # Act - Activate the ticket (payment completed)
        ticket.status = Ticket.TicketStatus.ACTIVE
        ticket.save()

        # Assert - User removed from waitlist
        assert not EventWaitList.objects.filter(event=public_event, user=member_user).exists()

    def test_user_not_on_waitlist_no_error(
        self,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that ticket activation works when user is not on waitlist.

        Creating/activating a ticket for a user who is not on the waitlist
        should work without errors.
        """
        # Arrange - User NOT on waitlist

        # Act - Create ACTIVE ticket
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
            tier=online_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Assert - No errors, ticket created successfully
        assert ticket.status == Ticket.TicketStatus.ACTIVE


class TestTicketCancellationNotifications:
    """Test waitlist notifications when tickets are cancelled."""

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_ticket_cancellation_notifies_waitlist_when_full(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that cancelling a ticket notifies waitlist when event was full.

        When an event is at capacity and a ticket is cancelled, users on the
        waitlist should be notified that a spot became available.
        """
        # Arrange - Fill event to capacity
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

        # Act - Cancel a ticket
        ticket1.status = Ticket.TicketStatus.CANCELLED
        ticket1.save()

        # Assert - Waitlist was notified
        assert mock_signal.called
        call_kwargs = mock_signal.call_args[1]
        assert call_kwargs["user"] == waitlist_user
        assert call_kwargs["notification_type"] == NotificationType.WAITLIST_SPOT_AVAILABLE
        assert call_kwargs["context"]["event_id"] == str(public_event.id)
        assert call_kwargs["context"]["spots_available"] == 1

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_ticket_cancellation_does_not_notify_when_not_full(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Test that cancelling a ticket doesn't notify when event wasn't full.

        If the event has available capacity before cancellation, waitlist
        should not be notified (they should have already been notified when
        the spot first became available).
        """
        # Arrange - Event NOT at capacity (only 1 ticket, capacity is 2)
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=public_event,
            user=member_user,
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

        mock_signal.reset_mock()

        # Act - Cancel the ticket (event was already not full)
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save()

        # Assert - Waitlist was NOT notified (event wasn't full before)
        # Filter for WAITLIST_SPOT_AVAILABLE notifications only (ignore TICKET_CANCELLED)
        waitlist_calls = [
            call
            for call in mock_signal.call_args_list
            if call[1].get("notification_type") == NotificationType.WAITLIST_SPOT_AVAILABLE
        ]
        assert len(waitlist_calls) == 0

    @patch("notifications.signals.waitlist.notification_requested.send")
    def test_ticket_deletion_notifies_waitlist(
        self,
        mock_signal: MagicMock,
        public_event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that deleting a ticket notifies waitlist when event was full.

        When a ticket is deleted and the event was at capacity, waitlist
        users should be notified.
        """
        # Arrange - Fill event to capacity
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

        # Act - Delete a ticket
        ticket1.delete()

        # Assert - Waitlist was notified
        assert mock_signal.called
        call_kwargs = mock_signal.call_args[1]
        assert call_kwargs["user"] == waitlist_user
        assert call_kwargs["notification_type"] == NotificationType.WAITLIST_SPOT_AVAILABLE
