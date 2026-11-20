"""Tests for notification eligibility service.

These tests verify feature parity between the BatchParticipationChecker (optimized batch lookups)
and the original per-user query functions, ensuring that the optimization doesn't change behavior.
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
    OrganizationStaff,
    Ticket,
    TicketTier,
)
from notifications.enums import NotificationType
from notifications.models import NotificationPreference
from notifications.service.eligibility import (
    BatchParticipationChecker,
    get_eligible_users_for_event_notification,
    has_active_rsvp,
    has_active_ticket,
    has_event_invitation,
    is_org_member,
    is_org_staff,
    is_participating_in_event,
    is_user_eligible_for_notification,
)


@pytest.mark.django_db
class TestBatchParticipationCheckerParity:
    """Tests verifying BatchParticipationChecker matches per-user functions."""

    def test_is_org_staff_parity(
        self,
        event: Event,
        organization: Organization,
        owner: RevelUser,
        staff_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_org_staff matches is_org_staff function."""
        OrganizationStaff.objects.create(organization=organization, user=staff_user)

        checker = BatchParticipationChecker(event)

        # Owner should be staff
        assert checker.is_org_staff(owner.id) == is_org_staff(owner, organization)
        assert checker.is_org_staff(owner.id) is True

        # Staff user should be staff
        assert checker.is_org_staff(staff_user.id) == is_org_staff(staff_user, organization)
        assert checker.is_org_staff(staff_user.id) is True

        # Random user should not be staff
        assert checker.is_org_staff(random_user.id) == is_org_staff(random_user, organization)
        assert checker.is_org_staff(random_user.id) is False

    def test_has_active_rsvp_parity(
        self,
        event: Event,
        rsvp_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.has_active_rsvp matches has_active_rsvp function."""
        EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.YES)

        checker = BatchParticipationChecker(event)

        # RSVP user should have active RSVP
        assert checker.has_active_rsvp(rsvp_user.id) == has_active_rsvp(rsvp_user, event)
        assert checker.has_active_rsvp(rsvp_user.id) is True

        # Random user should not have RSVP
        assert checker.has_active_rsvp(random_user.id) == has_active_rsvp(random_user, event)
        assert checker.has_active_rsvp(random_user.id) is False

    def test_has_active_rsvp_maybe_status(
        self,
        event: Event,
        rsvp_user: RevelUser,
    ) -> None:
        """MAYBE status counts as active RSVP."""
        EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.MAYBE)

        checker = BatchParticipationChecker(event)

        assert checker.has_active_rsvp(rsvp_user.id) == has_active_rsvp(rsvp_user, event)
        assert checker.has_active_rsvp(rsvp_user.id) is True

    def test_has_active_rsvp_no_status_excluded(
        self,
        event: Event,
        rsvp_user: RevelUser,
    ) -> None:
        """NO status does not count as active RSVP."""
        EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.NO)

        checker = BatchParticipationChecker(event)

        assert checker.has_active_rsvp(rsvp_user.id) == has_active_rsvp(rsvp_user, event)
        assert checker.has_active_rsvp(rsvp_user.id) is False

    def test_has_active_ticket_active_status(
        self,
        event: Event,
        ticket_user: RevelUser,
        ticket_tier: TicketTier,
        random_user: RevelUser,
    ) -> None:
        """Active tickets are counted."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Test",
        )

        checker = BatchParticipationChecker(event)

        assert checker.has_active_ticket(ticket_user.id) == has_active_ticket(ticket_user, event)
        assert checker.has_active_ticket(ticket_user.id) is True

        assert checker.has_active_ticket(random_user.id) == has_active_ticket(random_user, event)
        assert checker.has_active_ticket(random_user.id) is False

    def test_has_active_ticket_pending_offline_counts(
        self,
        event: Event,
        ticket_user: RevelUser,
        offline_tier: TicketTier,
    ) -> None:
        """Pending tickets for offline payment methods count as active."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=offline_tier,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test",
        )

        checker = BatchParticipationChecker(event)

        assert checker.has_active_ticket(ticket_user.id) == has_active_ticket(ticket_user, event)
        assert checker.has_active_ticket(ticket_user.id) is True

    def test_has_active_ticket_pending_online_excluded(
        self,
        event: Event,
        ticket_user: RevelUser,
        online_tier: TicketTier,
    ) -> None:
        """Pending tickets for online payment methods do NOT count as active."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=online_tier,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test",
        )

        checker = BatchParticipationChecker(event)

        assert checker.has_active_ticket(ticket_user.id) == has_active_ticket(ticket_user, event)
        assert checker.has_active_ticket(ticket_user.id) is False

    def test_has_active_ticket_cancelled_excluded(
        self,
        event: Event,
        ticket_user: RevelUser,
        ticket_tier: TicketTier,
    ) -> None:
        """Cancelled tickets do not count as active."""
        Ticket.objects.create(
            event=event,
            user=ticket_user,
            tier=ticket_tier,
            status=Ticket.TicketStatus.CANCELLED,
            guest_name="Test",
        )

        checker = BatchParticipationChecker(event)

        assert checker.has_active_ticket(ticket_user.id) == has_active_ticket(ticket_user, event)
        assert checker.has_active_ticket(ticket_user.id) is False

    def test_has_event_invitation_parity(
        self,
        event: Event,
        invited_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.has_event_invitation matches has_event_invitation."""
        EventInvitation.objects.create(event=event, user=invited_user)

        checker = BatchParticipationChecker(event)

        assert checker.has_event_invitation(invited_user.id) == has_event_invitation(invited_user, event)
        assert checker.has_event_invitation(invited_user.id) is True

        assert checker.has_event_invitation(random_user.id) == has_event_invitation(random_user, event)
        assert checker.has_event_invitation(random_user.id) is False

    def test_is_org_member_parity(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
        random_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_org_member matches is_org_member."""
        OrganizationMember.objects.create(organization=organization, user=member_user)

        checker = BatchParticipationChecker(event)

        assert checker.is_org_member(member_user.id) == is_org_member(member_user, organization)
        assert checker.is_org_member(member_user.id) is True

        assert checker.is_org_member(random_user.id) == is_org_member(random_user, organization)
        assert checker.is_org_member(random_user.id) is False

    def test_is_org_member_excludes_cancelled(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_org_member returns False for CANCELLED members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        checker = BatchParticipationChecker(event)

        assert checker.is_org_member(member_user.id) is False
        assert is_org_member(member_user, organization) is False

    def test_is_org_member_excludes_banned(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_org_member returns False for BANNED members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )

        checker = BatchParticipationChecker(event)

        assert checker.is_org_member(member_user.id) is False
        assert is_org_member(member_user, organization) is False

    def test_is_org_member_includes_paused(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_org_member returns True for PAUSED members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )

        checker = BatchParticipationChecker(event)

        assert checker.is_org_member(member_user.id) is True
        assert is_org_member(member_user, organization) is True

    def test_is_participating_parity(
        self,
        event: Event,
        owner: RevelUser,
        setup_participants: dict[str, RevelUser],
        random_user: RevelUser,
    ) -> None:
        """BatchParticipationChecker.is_participating matches is_participating_in_event."""
        checker = BatchParticipationChecker(event)

        # Owner is participating (as org staff)
        assert checker.is_participating(owner.id) == is_participating_in_event(owner, event)
        assert checker.is_participating(owner.id) is True

        # All participants should be participating
        for role, user in setup_participants.items():
            assert checker.is_participating(user.id) == is_participating_in_event(user, event), f"Failed for {role}"
            assert checker.is_participating(user.id) is True, f"Failed for {role}"

        # Random user should not be participating
        assert checker.is_participating(random_user.id) == is_participating_in_event(random_user, event)
        assert checker.is_participating(random_user.id) is False


@pytest.mark.django_db
class TestEligibilityWithBatchChecker:
    """Tests for is_user_eligible_for_notification with batch checker."""

    def test_eligible_with_batch_checker(
        self,
        event: Event,
        owner: RevelUser,
        setup_participants: dict[str, RevelUser],
    ) -> None:
        """Users with participation are eligible when using batch checker."""
        batch_checker = BatchParticipationChecker(event)

        # Owner should be eligible
        assert (
            is_user_eligible_for_notification(
                owner,
                NotificationType.EVENT_UPDATED,
                event=event,
                batch_checker=batch_checker,
            )
            is True
        )

        # All participants should be eligible
        for role, user in setup_participants.items():
            assert (
                is_user_eligible_for_notification(
                    user,
                    NotificationType.EVENT_UPDATED,
                    event=event,
                    batch_checker=batch_checker,
                )
                is True
            ), f"Failed for {role}"

    def test_ineligible_with_batch_checker(
        self,
        event: Event,
        random_user: RevelUser,
    ) -> None:
        """Users without participation are not eligible."""
        batch_checker = BatchParticipationChecker(event)

        assert (
            is_user_eligible_for_notification(
                random_user,
                NotificationType.EVENT_UPDATED,
                event=event,
                batch_checker=batch_checker,
            )
            is False
        )

    def test_silenced_user_not_eligible(
        self,
        event: Event,
        owner: RevelUser,
    ) -> None:
        """Users with silenced notifications are not eligible."""
        prefs = NotificationPreference.objects.get(user=owner)
        prefs.silence_all_notifications = True
        prefs.save()

        # Refresh user from database to clear cached notification_preferences
        owner.refresh_from_db()

        batch_checker = BatchParticipationChecker(event)

        assert (
            is_user_eligible_for_notification(
                owner,
                NotificationType.EVENT_UPDATED,
                event=event,
                batch_checker=batch_checker,
            )
            is False
        )

    def test_disabled_notification_type_not_eligible(
        self,
        event: Event,
        owner: RevelUser,
    ) -> None:
        """Users with disabled notification type are not eligible."""
        prefs = NotificationPreference.objects.get(user=owner)
        prefs.notification_type_settings = {NotificationType.EVENT_UPDATED.value: {"enabled": False}}
        prefs.save()

        # Refresh user from database to clear cached notification_preferences
        owner.refresh_from_db()

        batch_checker = BatchParticipationChecker(event)

        assert (
            is_user_eligible_for_notification(
                owner,
                NotificationType.EVENT_UPDATED,
                event=event,
                batch_checker=batch_checker,
            )
            is False
        )


@pytest.mark.django_db
class TestGetEligibleUsersOptimization:
    """Tests for get_eligible_users_for_event_notification with batch optimization."""

    def test_returns_correct_users(
        self,
        event: Event,
        owner: RevelUser,
        setup_participants: dict[str, RevelUser],
        random_user: RevelUser,
    ) -> None:
        """get_eligible_users_for_event_notification returns correct users."""
        eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED))
        eligible_ids = {u.id for u in eligible_users}

        # Owner should be eligible
        assert owner.id in eligible_ids

        # All participants should be eligible
        for role, user in setup_participants.items():
            assert user.id in eligible_ids, f"{role} should be eligible"

        # Random user should not be eligible
        assert random_user.id not in eligible_ids

    def test_excludes_silenced_users(
        self,
        event: Event,
        owner: RevelUser,
    ) -> None:
        """Silenced users are excluded from results."""
        prefs = NotificationPreference.objects.get(user=owner)
        prefs.silence_all_notifications = True
        prefs.save()

        eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_UPDATED))
        eligible_ids = {u.id for u in eligible_users}

        assert owner.id not in eligible_ids

    def test_event_open_includes_members(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """EVENT_OPEN notifications include organization members."""
        OrganizationMember.objects.create(organization=organization, user=member_user)

        eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN))
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id in eligible_ids

    def test_event_open_excludes_cancelled_members(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """EVENT_OPEN notifications exclude CANCELLED members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN))
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id not in eligible_ids

    def test_event_open_excludes_banned_members(
        self,
        event: Event,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """EVENT_OPEN notifications exclude BANNED members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )

        eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN))
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id not in eligible_ids

    def test_members_only_event_includes_active_members(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """MEMBERS_ONLY events include ACTIVE members in EVENT_OPEN notifications."""
        next_week = timezone.now() + timedelta(days=7)
        members_only_event = Event.objects.create(
            organization=organization,
            name="Members Only Event",
            slug="members-only-event",
            visibility=Event.Visibility.MEMBERS_ONLY,
            event_type=Event.EventType.MEMBERS_ONLY,
            status=Event.EventStatus.OPEN,
            start=next_week,
            end=next_week + timedelta(hours=2),
        )
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        eligible_users = list(
            get_eligible_users_for_event_notification(members_only_event, NotificationType.EVENT_OPEN)
        )
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id in eligible_ids

    def test_members_only_event_excludes_cancelled_members(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """MEMBERS_ONLY events exclude CANCELLED members from EVENT_OPEN notifications."""
        next_week = timezone.now() + timedelta(days=7)
        members_only_event = Event.objects.create(
            organization=organization,
            name="Members Only Event",
            slug="members-only-event-cancelled",
            visibility=Event.Visibility.MEMBERS_ONLY,
            event_type=Event.EventType.MEMBERS_ONLY,
            status=Event.EventStatus.OPEN,
            start=next_week,
            end=next_week + timedelta(hours=2),
        )
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        eligible_users = list(
            get_eligible_users_for_event_notification(members_only_event, NotificationType.EVENT_OPEN)
        )
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id not in eligible_ids

    def test_members_only_event_excludes_banned_members(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """MEMBERS_ONLY events exclude BANNED members from EVENT_OPEN notifications."""
        next_week = timezone.now() + timedelta(days=7)
        members_only_event = Event.objects.create(
            organization=organization,
            name="Members Only Event",
            slug="members-only-event-banned",
            visibility=Event.Visibility.MEMBERS_ONLY,
            event_type=Event.EventType.MEMBERS_ONLY,
            status=Event.EventStatus.OPEN,
            start=next_week,
            end=next_week + timedelta(hours=2),
        )
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )

        eligible_users = list(
            get_eligible_users_for_event_notification(members_only_event, NotificationType.EVENT_OPEN)
        )
        eligible_ids = {u.id for u in eligible_users}

        assert member_user.id not in eligible_ids
