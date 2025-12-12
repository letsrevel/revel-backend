"""Tests for notification service functions."""

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, Organization, OrganizationMember, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.models import NotificationPreference
from notifications.service.eligibility import (
    get_eligible_users_for_event_notification,
    has_active_rsvp,
    has_active_ticket,
    has_event_invitation,
    is_org_member,
    is_org_staff,
    is_participating_in_event,
    is_user_eligible_for_notification,
)

pytestmark = pytest.mark.django_db


class TestParticipationHelpers:
    """Test helper functions for checking participation."""

    def test_has_active_rsvp_yes(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test detection of YES RSVP."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        assert has_active_rsvp(nonmember_user, event)

    def test_has_active_rsvp_maybe(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test detection of MAYBE RSVP."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.MAYBE)
        assert has_active_rsvp(nonmember_user, event)

    def test_has_active_rsvp_no(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that NO RSVP is not considered active."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.NO)
        assert not has_active_rsvp(nonmember_user, event)

    def test_has_active_ticket_with_active_ticket(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test detection of active ticket."""
        tier = TicketTier.objects.create(event=event, name="General", price=10, payment_method="online")
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE
        )
        assert has_active_ticket(nonmember_user, event)

    def test_has_active_ticket_with_pending_offline(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that pending tickets count for offline payment tiers."""
        tier = TicketTier.objects.create(event=event, name="General", price=10, payment_method="offline")
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.PENDING
        )
        assert has_active_ticket(nonmember_user, event)

    def test_has_active_ticket_with_pending_online(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that pending tickets don't count for online payment tiers."""
        tier = TicketTier.objects.create(event=event, name="General", price=10, payment_method="online")
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.PENDING
        )
        assert not has_active_ticket(nonmember_user, event)

    def test_has_event_invitation(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test detection of event invitation."""
        EventInvitation.objects.create(user=nonmember_user, event=event)
        assert has_event_invitation(nonmember_user, event)

    def test_is_org_member(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test detection of organization membership."""
        OrganizationMember.objects.create(user=nonmember_user, organization=organization)
        assert is_org_member(nonmember_user, organization)

    def test_is_org_staff(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test detection of organization staff."""
        organization.staff_members.add(nonmember_user)
        assert is_org_staff(nonmember_user, organization)

    def test_is_org_staff_owner(self, organization: Organization) -> None:
        """Test detection of organization owner."""
        assert is_org_staff(organization.owner, organization)


class TestIsParticipatingInEvent:
    """Test is_participating_in_event function."""

    def test_participating_via_rsvp(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test participation through RSVP."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        assert is_participating_in_event(nonmember_user, event)

    def test_participating_via_ticket(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test participation through ticket."""
        tier = TicketTier.objects.create(event=event, name="General", price=10)
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE
        )
        assert is_participating_in_event(nonmember_user, event)

    def test_participating_via_invitation(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test participation through invitation."""
        EventInvitation.objects.create(user=nonmember_user, event=event)
        assert is_participating_in_event(nonmember_user, event)

    def test_participating_as_staff(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test participation as organization staff."""
        event.organization.staff_members.add(nonmember_user)
        assert is_participating_in_event(nonmember_user, event)

    def test_not_participating(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test non-participating user."""
        assert not is_participating_in_event(nonmember_user, event)


class TestIsUserEligibleForNotification:
    """Test is_user_eligible_for_notification function."""

    def test_eligible_with_rsvp(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test eligibility with RSVP."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        assert is_user_eligible_for_notification(nonmember_user, NotificationType.EVENT_UPDATED, event=event)

    def test_not_eligible_silenced(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that silenced users are not eligible."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        prefs.silence_all_notifications = True
        prefs.save()
        assert not is_user_eligible_for_notification(nonmember_user, NotificationType.EVENT_UPDATED, event=event)

    def test_not_eligible_notification_type_disabled(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with disabled notification type are not eligible."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        prefs.notification_type_settings = {NotificationType.EVENT_UPDATED: {"enabled": False}}
        prefs.save()
        assert not is_user_eligible_for_notification(nonmember_user, NotificationType.EVENT_UPDATED, event=event)

    def test_not_eligible_not_participating(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that non-participating users are not eligible."""
        assert not is_user_eligible_for_notification(nonmember_user, NotificationType.EVENT_UPDATED, event=event)


class TestGetEligibleUsersForEventNotification:
    """Test get_eligible_users_for_event_notification function."""

    def test_includes_users_with_rsvp(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with RSVP are included."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user in eligible_users

    def test_includes_users_with_tickets(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with tickets are included."""
        tier = TicketTier.objects.create(event=event, name="General", price=10)
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE
        )
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user in eligible_users

    def test_includes_users_with_invitations(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with invitations are included."""
        EventInvitation.objects.create(user=nonmember_user, event=event)
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user in eligible_users

    def test_includes_org_members_for_event_open(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that org members are included for EVENT_OPEN notifications."""
        OrganizationMember.objects.create(user=nonmember_user, organization=event.organization)
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)
        assert nonmember_user in eligible_users

    def test_excludes_org_members_for_other_notifications(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that org members without participation are excluded for non-EVENT_OPEN."""
        OrganizationMember.objects.create(user=nonmember_user, organization=event.organization)
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user not in eligible_users

    def test_excludes_users_who_silenced_notifications(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users who silenced all notifications are excluded."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        prefs.silence_all_notifications = True
        prefs.save()

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user not in eligible_users

    def test_excludes_users_with_notification_type_disabled(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users with disabled notification type are excluded."""
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        prefs.notification_type_settings = {NotificationType.EVENT_UPDATED: {"enabled": False}}
        prefs.save()

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)
        assert nonmember_user not in eligible_users

    def test_returns_unique_users(self, event: Event, nonmember_user: RevelUser) -> None:
        """Test that users are not duplicated if they match multiple criteria."""
        # Create multiple participation types for same user
        EventRSVP.objects.create(user=nonmember_user, event=event, status=EventRSVP.RsvpStatus.YES)
        EventInvitation.objects.create(user=nonmember_user, event=event)
        tier = TicketTier.objects.create(event=event, name="General", price=10)
        Ticket.objects.create(
            guest_name="Test Guest", user=nonmember_user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE
        )

        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED)

        # User should appear only once
        user_ids = list(eligible_users.values_list("id", flat=True))
        assert user_ids.count(nonmember_user.id) == 1
