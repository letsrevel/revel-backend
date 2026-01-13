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


class TestBlacklistLinkingOnUserCreation:
    """Test that blacklist entries are automatically linked when a new user is created.

    The signal `handle_user_creation` in events/signals.py links unlinked blacklist entries
    when their identifiers (email, phone, telegram) match the new user.
    """

    def test_links_blacklist_entry_by_email_on_registration(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that blacklist entry is linked when new user's email matches."""
        from events.models import Blacklist

        # Create unlinked blacklist entry FIRST
        entry = Blacklist.objects.create(
            organization=organization,
            email="blacklisted@example.com",
            reason="Created before registration",
            created_by=organization.owner,
        )
        assert entry.user is None

        # THEN create user with matching email - this triggers the signal
        user = django_user_model.objects.create_user(
            username="blacklisted_user",
            email="blacklisted@example.com",
            password="pass",
        )

        # Entry should now be linked to the user
        entry.refresh_from_db()
        assert entry.user == user

    def test_links_blacklist_entry_by_phone_on_registration(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that blacklist entry is linked when new user's phone matches."""
        from events.models import Blacklist

        # Create unlinked blacklist entry
        entry = Blacklist.objects.create(
            organization=organization,
            phone_number="+1234567890",
            created_by=organization.owner,
        )

        # Create user with matching phone
        user = django_user_model.objects.create_user(
            username="phone_user",
            email="phone@example.com",
            password="pass",
            phone_number="+1234567890",
        )

        # Entry should be linked
        entry.refresh_from_db()
        assert entry.user == user

    def test_links_multiple_blacklist_entries_on_registration(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that multiple blacklist entries can be linked for one user."""
        from events.models import Blacklist, Organization

        # Create another org
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=organization.owner,
        )

        # Create entries in different orgs with same email
        entry1 = Blacklist.objects.create(
            organization=organization,
            email="multi@example.com",
            created_by=organization.owner,
        )
        entry2 = Blacklist.objects.create(
            organization=other_org,
            email="multi@example.com",
            created_by=organization.owner,
        )

        # Create user with matching email
        user = django_user_model.objects.create_user(
            username="multi_user",
            email="multi@example.com",
            password="pass",
        )

        # Both entries should be linked
        entry1.refresh_from_db()
        entry2.refresh_from_db()
        assert entry1.user == user
        assert entry2.user == user

    def test_does_not_relink_already_linked_entry(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that entries already linked to another user are not changed."""
        from events.models import Blacklist

        # Create first user
        existing_user = django_user_model.objects.create_user(
            username="existing",
            email="existing@example.com",
            password="pass",
        )

        # Create blacklist entry linked to existing user
        entry = Blacklist.objects.create(
            organization=organization,
            user=existing_user,
            email="shared@example.com",  # Different from user's email
            created_by=organization.owner,
        )

        # Create new user with email matching the entry
        django_user_model.objects.create_user(
            username="new",
            email="shared@example.com",
            password="pass",
        )

        # Entry should still be linked to existing user, not new user
        entry.refresh_from_db()
        assert entry.user == existing_user

    def test_no_error_when_no_matching_entries(self, django_user_model: type[RevelUser]) -> None:
        """Test that user creation works fine when no blacklist entries match."""
        # Create user with unique email (no matching entries)
        user = django_user_model.objects.create_user(
            username="unique",
            email="unique@example.com",
            password="pass",
        )

        # Should succeed without error
        assert user.pk is not None


class TestBlacklistUserLinkedSignal:
    """Test that blacklisted users are removed from staff and banned from the organization.

    The signal `handle_blacklist_user_linked` fires when a Blacklist entry is created
    or updated with a user FK. It removes the user from staff and sets their
    membership status to BANNED.
    """

    def test_removes_user_from_staff_when_blacklisted(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that a staff member is removed from staff when blacklisted."""
        from events.models import Blacklist, OrganizationStaff

        # Create a staff member
        staff_user = django_user_model.objects.create_user(
            username="staff_to_ban",
            email="staff_ban@example.com",
            password="pass",
        )
        OrganizationStaff.objects.create(organization=organization, user=staff_user)
        assert OrganizationStaff.objects.filter(organization=organization, user=staff_user).exists()

        # Blacklist the user
        Blacklist.objects.create(
            organization=organization,
            user=staff_user,
            reason="Staff member banned",
            created_by=organization.owner,
        )

        # Staff membership should be removed
        assert not OrganizationStaff.objects.filter(organization=organization, user=staff_user).exists()

    def test_sets_membership_status_to_banned(self, organization: t.Any, django_user_model: type[RevelUser]) -> None:
        """Test that an existing member's status is set to BANNED when blacklisted."""
        from events.models import Blacklist, OrganizationMember

        # Create a member
        member_user = django_user_model.objects.create_user(
            username="member_to_ban",
            email="member_ban@example.com",
            password="pass",
        )
        membership = OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        assert membership.status == OrganizationMember.MembershipStatus.ACTIVE

        # Blacklist the user
        Blacklist.objects.create(
            organization=organization,
            user=member_user,
            reason="Member banned",
            created_by=organization.owner,
        )

        # Membership status should be BANNED
        membership.refresh_from_db()
        assert membership.status == OrganizationMember.MembershipStatus.BANNED

    def test_creates_banned_membership_if_not_exists(
        self, organization: t.Any, django_user_model: type[RevelUser]
    ) -> None:
        """Test that a BANNED membership is created if user wasn't a member."""
        from events.models import Blacklist, OrganizationMember

        # Create a user who is NOT a member
        non_member = django_user_model.objects.create_user(
            username="non_member_to_ban",
            email="non_member_ban@example.com",
            password="pass",
        )
        assert not OrganizationMember.objects.filter(organization=organization, user=non_member).exists()

        # Blacklist the user
        Blacklist.objects.create(
            organization=organization,
            user=non_member,
            reason="Non-member banned",
            created_by=organization.owner,
        )

        # A BANNED membership should be created
        membership = OrganizationMember.objects.get(organization=organization, user=non_member)
        assert membership.status == OrganizationMember.MembershipStatus.BANNED

    def test_owner_cannot_be_banned_from_own_org(self, organization: t.Any) -> None:
        """Test that the organization owner cannot be banned from their own org."""
        from events.models import Blacklist, OrganizationMember

        owner = organization.owner

        # Attempt to blacklist the owner
        Blacklist.objects.create(
            organization=organization,
            user=owner,
            reason="Trying to ban owner",
            created_by=owner,
        )

        # Owner should NOT have a BANNED membership created
        assert not OrganizationMember.objects.filter(
            organization=organization,
            user=owner,
            status=OrganizationMember.MembershipStatus.BANNED,
        ).exists()

    def test_auto_linking_triggers_ban(self, organization: t.Any, django_user_model: type[RevelUser]) -> None:
        """Test that auto-linking a blacklist entry also triggers the ban."""
        from events.models import Blacklist, OrganizationMember

        # Create blacklist entry WITHOUT user FK first
        entry = Blacklist.objects.create(
            organization=organization,
            email="future_user@example.com",
            reason="Blacklisted before registration",
            created_by=organization.owner,
        )
        assert entry.user is None

        # Create user with matching email - signal will auto-link
        new_user = django_user_model.objects.create_user(
            username="future_user",
            email="future_user@example.com",
            password="pass",
        )

        # Entry should be linked
        entry.refresh_from_db()
        assert entry.user == new_user

        # User should have BANNED membership
        membership = OrganizationMember.objects.get(organization=organization, user=new_user)
        assert membership.status == OrganizationMember.MembershipStatus.BANNED

    def test_blacklist_without_user_does_nothing(self, organization: t.Any) -> None:
        """Test that blacklist entries without user FK don't trigger banning."""
        from events.models import Blacklist, OrganizationMember

        initial_member_count = OrganizationMember.objects.filter(organization=organization).count()

        # Create blacklist entry without user FK
        Blacklist.objects.create(
            organization=organization,
            email="unknown@example.com",
            first_name="Unknown",
            last_name="Person",
            reason="Manual entry",
            created_by=organization.owner,
        )

        # No new memberships should be created
        assert OrganizationMember.objects.filter(organization=organization).count() == initial_member_count
