"""Tests for announcement recipient resolution and eligibility.

This module tests the recipient logic and visibility checks for announcements.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    Event,
    EventRSVP,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    Ticket,
    TicketTier,
)
from events.service import announcement_service
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestGetRecipients:
    """Tests for get_recipients function and recipient resolution logic."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    @pytest.fixture
    def free_tier(self, event: Event) -> TicketTier:
        """Free ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Free Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def at_door_tier(self, event: Event) -> TicketTier:
        """At the door payment tier."""
        return TicketTier.objects.create(
            event=event,
            name="At Door Tier",
            payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """VIP membership tier."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )

    def test_get_recipients_for_event_includes_active_ticket_holders(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        free_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that event recipients include users with ACTIVE tickets."""
        # Arrange
        ticket_holder = revel_user_factory(username="ticket_holder")
        Ticket.objects.create(
            event=event,
            user=ticket_holder,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Ticket Holder",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert ticket_holder in recipients

    def test_get_recipients_for_event_includes_checked_in_ticket_holders(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        free_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that event recipients include users with CHECKED_IN tickets."""
        # Arrange
        ticket_holder = revel_user_factory(username="checked_in_holder")
        Ticket.objects.create(
            event=event,
            user=ticket_holder,
            tier=free_tier,
            status=Ticket.TicketStatus.CHECKED_IN,
            guest_name="Checked In User",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert ticket_holder in recipients

    def test_get_recipients_for_event_includes_at_door_pending_tickets(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        at_door_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that AT_THE_DOOR pending tickets are included."""
        # Arrange
        at_door_holder = revel_user_factory(username="at_door_holder")
        Ticket.objects.create(
            event=event,
            user=at_door_holder,
            tier=at_door_tier,
            status=Ticket.TicketStatus.PENDING,
            guest_name="At Door User",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert at_door_holder in recipients

    def test_get_recipients_for_event_excludes_regular_pending_tickets(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        free_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that regular PENDING tickets are excluded."""
        # Arrange
        pending_holder = revel_user_factory(username="pending_holder")
        Ticket.objects.create(
            event=event,
            user=pending_holder,
            tier=free_tier,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Pending User",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert pending_holder not in recipients

    def test_get_recipients_for_event_includes_yes_rsvps(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that users with YES RSVPs are included."""
        # Arrange
        rsvp_user = revel_user_factory(username="rsvp_user")
        EventRSVP.objects.create(
            event=event,
            user=rsvp_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert rsvp_user in recipients

    def test_get_recipients_for_event_excludes_no_rsvps(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that users with NO/MAYBE RSVPs are excluded."""
        # Arrange
        no_user = revel_user_factory(username="no_user")
        EventRSVP.objects.create(
            event=event,
            user=no_user,
            status=EventRSVP.RsvpStatus.NO,
        )

        maybe_user = revel_user_factory(username="maybe_user")
        EventRSVP.objects.create(
            event=event,
            user=maybe_user,
            status=EventRSVP.RsvpStatus.MAYBE,
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert no_user not in recipients
        assert maybe_user not in recipients

    def test_get_recipients_for_all_members_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that all active organization members are included."""
        # Arrange
        active_member = revel_user_factory(username="active_member")
        OrganizationMember.objects.create(
            organization=org,
            user=active_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        inactive_member = revel_user_factory(username="inactive_member")
        OrganizationMember.objects.create(
            organization=org,
            user=inactive_member,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )

        announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert active_member in recipients
        assert inactive_member not in recipients

    def test_get_recipients_for_tier_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that only members of specified tiers are included."""
        # Arrange
        vip_member = revel_user_factory(username="vip_member")
        OrganizationMember.objects.create(
            organization=org,
            user=vip_member,
            tier=membership_tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        regular_member = revel_user_factory(username="regular_member")
        OrganizationMember.objects.create(
            organization=org,
            user=regular_member,
            tier=None,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        announcement = Announcement.objects.create(
            organization=org,
            title="VIP Announcement",
            body="Body",
            created_by=org_owner,
        )
        announcement.target_tiers.add(membership_tier)

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert vip_member in recipients
        assert regular_member not in recipients

    def test_get_recipients_for_staff_only_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that only organization staff are included."""
        # Arrange
        staff_member = revel_user_factory(username="staff_member")
        OrganizationStaff.objects.create(
            organization=org,
            user=staff_member,
            permissions=PermissionsSchema(default=PermissionMap()).model_dump(mode="json"),
        )

        regular_member = revel_user_factory(username="regular_member")
        OrganizationMember.objects.create(
            organization=org,
            user=regular_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        announcement = Announcement.objects.create(
            organization=org,
            title="Staff Announcement",
            body="Body",
            target_staff_only=True,
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert staff_member in recipients
        assert regular_member not in recipients

    def test_get_recipients_returns_empty_for_no_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that no recipients are returned when no targeting is set."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="No Target Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = announcement_service.get_recipients(announcement)

        # Assert
        assert recipients.count() == 0


class TestIsUserEligibleForAnnouncement:
    """Tests for is_user_eligible_for_announcement function."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def member(self, org: Organization, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    def test_user_with_notification_is_eligible(
        self,
        org: Organization,
        org_owner: RevelUser,
        member: RevelUser,
    ) -> None:
        """Test that user who received notification is eligible."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(announcement.id)},
        )

        # Act
        is_eligible = announcement_service.is_user_eligible_for_announcement(announcement, member)

        # Assert
        assert is_eligible is True

    def test_new_member_eligible_with_past_visibility(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new member is eligible if past_visibility is enabled."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body",
            target_all_members=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        new_member = revel_user_factory(username="new_member")
        OrganizationMember.objects.create(
            organization=org,
            user=new_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        # Act
        is_eligible = announcement_service.is_user_eligible_for_announcement(announcement, new_member)

        # Assert
        assert is_eligible is True

    def test_new_member_not_eligible_without_past_visibility(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new member is not eligible if past_visibility is disabled."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body",
            target_all_members=True,
            past_visibility=False,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        new_member = revel_user_factory(username="new_member")
        OrganizationMember.objects.create(
            organization=org,
            user=new_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        # Act
        is_eligible = announcement_service.is_user_eligible_for_announcement(announcement, new_member)

        # Assert
        assert is_eligible is False

    def test_draft_announcement_not_eligible(
        self,
        org: Organization,
        org_owner: RevelUser,
        member: RevelUser,
    ) -> None:
        """Test that draft announcements are never visible."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        # Act
        is_eligible = announcement_service.is_user_eligible_for_announcement(announcement, member)

        # Assert
        assert is_eligible is False

    def test_non_member_not_eligible_for_member_announcement(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that non-members cannot see member announcements."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        non_member = revel_user_factory(username="non_member")

        # Act
        is_eligible = announcement_service.is_user_eligible_for_announcement(announcement, non_member)

        # Assert
        assert is_eligible is False


class TestRecipientDeduplication:
    """Tests for recipient deduplication logic."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    @pytest.fixture
    def free_tier(self, event: Event) -> TicketTier:
        """Free ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Free",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    def test_user_with_ticket_and_rsvp_appears_once(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        free_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that user with both ticket and RSVP is counted once."""
        # Arrange
        user = revel_user_factory(username="dual_user")

        Ticket.objects.create(
            event=event,
            user=user,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="User",
        )
        EventRSVP.objects.create(
            event=event,
            user=user,
            status=EventRSVP.RsvpStatus.YES,
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        recipients = list(announcement_service.get_recipients(announcement))

        # Assert
        assert len(recipients) == 1
        assert user in recipients
