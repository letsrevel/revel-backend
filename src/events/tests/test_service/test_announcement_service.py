"""Tests for announcement_service functions.

This module tests the announcement service layer including CRUD operations,
notification dispatch, and recipient count.

For recipient resolution and visibility tests, see test_announcement_recipients.py.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    Event,
    MembershipTier,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.schema.announcement import AnnouncementCreateSchema, AnnouncementUpdateSchema
from events.service import announcement_service
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestCreateAnnouncement:
    """Tests for create_announcement function."""

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
        """Test event in the organization."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """Membership tier fixture."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )

    def test_create_announcement_with_event_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
    ) -> None:
        """Test creating announcement targeting event attendees."""
        # Arrange
        payload = AnnouncementCreateSchema(
            title="Event Announcement",
            body="Hello attendees!",
            event_id=event.id,
            past_visibility=True,
        )

        # Act
        announcement = announcement_service.create_announcement(org, org_owner, payload)

        # Assert
        assert announcement.organization == org
        assert announcement.event == event
        assert announcement.title == "Event Announcement"
        assert announcement.body == "Hello attendees!"
        assert announcement.status == Announcement.AnnouncementStatus.DRAFT
        assert announcement.created_by == org_owner
        assert announcement.past_visibility is True

    def test_create_announcement_with_all_members_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test creating announcement targeting all organization members."""
        # Arrange
        payload = AnnouncementCreateSchema(
            title="Members Announcement",
            body="Hello members!",
            target_all_members=True,
        )

        # Act
        announcement = announcement_service.create_announcement(org, org_owner, payload)

        # Assert
        assert announcement.target_all_members is True
        assert announcement.event is None
        assert announcement.target_staff_only is False

    def test_create_announcement_with_tier_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
    ) -> None:
        """Test creating announcement targeting specific membership tiers."""
        # Arrange
        payload = AnnouncementCreateSchema(
            title="VIP Announcement",
            body="Hello VIPs!",
            target_tier_ids=[membership_tier.id],
        )

        # Act
        announcement = announcement_service.create_announcement(org, org_owner, payload)

        # Assert
        assert membership_tier in announcement.target_tiers.all()
        assert announcement.target_all_members is False

    def test_create_announcement_with_staff_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test creating announcement targeting only staff members."""
        # Arrange
        payload = AnnouncementCreateSchema(
            title="Staff Announcement",
            body="Hello staff!",
            target_staff_only=True,
        )

        # Act
        announcement = announcement_service.create_announcement(org, org_owner, payload)

        # Assert
        assert announcement.target_staff_only is True
        assert announcement.target_all_members is False
        assert announcement.event is None

    def test_create_announcement_with_invalid_event_raises_error(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that creating announcement with non-existent event raises error."""
        # Arrange
        payload = AnnouncementCreateSchema(
            title="Bad Announcement",
            body="This should fail",
            event_id=uuid4(),
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            announcement_service.create_announcement(org, org_owner, payload)

        assert "Event not found" in str(exc_info.value)

    def test_create_announcement_with_event_from_different_org_raises_error(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that event must belong to the same organization."""
        # Arrange
        other_owner = revel_user_factory(username="other_owner")
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=other_owner,
        )
        other_event = Event.objects.create(
            organization=other_org,
            name="Other Event",
            slug="other-event",
            start=timezone.now(),
        )

        payload = AnnouncementCreateSchema(
            title="Cross-org Announcement",
            body="This should fail",
            event_id=other_event.id,
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            announcement_service.create_announcement(org, org_owner, payload)

        assert "does not belong to this organization" in str(exc_info.value)

    def test_create_announcement_filters_tiers_from_other_orgs(
        self,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that only tiers from the same organization are added."""
        # Arrange
        other_owner = revel_user_factory(username="other_owner")
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=other_owner,
        )
        other_tier = MembershipTier.objects.create(
            organization=other_org,
            name="Other Tier",
        )

        payload = AnnouncementCreateSchema(
            title="Mixed Tier Announcement",
            body="Body",
            target_tier_ids=[membership_tier.id, other_tier.id],
        )

        # Act
        announcement = announcement_service.create_announcement(org, org_owner, payload)

        # Assert
        tiers = list(announcement.target_tiers.all())
        assert membership_tier in tiers
        assert other_tier not in tiers


class TestUpdateAnnouncement:
    """Tests for update_announcement function."""

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
        )

    @pytest.fixture
    def draft_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Draft announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Original body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )

    @pytest.fixture
    def sent_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Sent announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Original body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
        )

    def test_update_announcement_title_and_body(
        self,
        draft_announcement: Announcement,
    ) -> None:
        """Test updating announcement title and body."""
        # Arrange
        payload = AnnouncementUpdateSchema(
            title="Updated Title",
            body="Updated body content",
        )

        # Act
        updated = announcement_service.update_announcement(draft_announcement, payload)

        # Assert
        assert updated.title == "Updated Title"
        assert updated.body == "Updated body content"

    def test_update_announcement_targeting_to_event(
        self,
        draft_announcement: Announcement,
        event: Event,
    ) -> None:
        """Test updating announcement to target an event."""
        # Arrange
        payload = AnnouncementUpdateSchema(
            event_id=event.id,
            target_all_members=False,
        )

        # Act
        updated = announcement_service.update_announcement(draft_announcement, payload)

        # Assert
        assert updated.event == event
        assert updated.target_all_members is False

    def test_update_announcement_clear_event_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
    ) -> None:
        """Test clearing event targeting from announcement."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        payload = AnnouncementUpdateSchema(target_all_members=True)

        # Act
        updated = announcement_service.update_announcement(announcement, payload)

        # Assert
        updated.refresh_from_db()
        assert updated.target_all_members is True

    def test_update_sent_announcement_raises_error(
        self,
        sent_announcement: Announcement,
    ) -> None:
        """Test that updating a sent announcement raises an error."""
        # Arrange
        payload = AnnouncementUpdateSchema(
            title="Should Not Update",
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            announcement_service.update_announcement(sent_announcement, payload)

        assert "Only draft announcements can be updated" in str(exc_info.value)

    def test_update_announcement_with_invalid_event_raises_error(
        self,
        draft_announcement: Announcement,
    ) -> None:
        """Test updating with non-existent event raises error."""
        # Arrange
        payload = AnnouncementUpdateSchema(
            event_id=uuid4(),
        )

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            announcement_service.update_announcement(draft_announcement, payload)

        assert "Event not found" in str(exc_info.value)


class TestGetRecipientCount:
    """Tests for get_recipient_count function."""

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

    def test_get_recipient_count_returns_correct_count(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that recipient count matches actual recipients."""
        # Arrange
        for i in range(3):
            user = revel_user_factory(username=f"member_{i}")
            OrganizationMember.objects.create(
                organization=org,
                user=user,
                status=OrganizationMember.MembershipStatus.ACTIVE,
            )

        announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        # Act
        count = announcement_service.get_recipient_count(announcement)

        # Assert
        assert count == 3


class TestSendAnnouncement:
    """Tests for send_announcement function."""

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
    def draft_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Draft announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )

    @pytest.fixture
    def sent_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Already sent announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
        )

    def test_send_announcement_updates_status(
        self,
        draft_announcement: Announcement,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that sending updates announcement status to SENT."""
        # Arrange
        member = revel_user_factory(username="member")
        OrganizationMember.objects.create(
            organization=org,
            user=member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        # Act
        announcement_service.send_announcement(draft_announcement)

        # Assert
        draft_announcement.refresh_from_db()
        assert draft_announcement.status == Announcement.AnnouncementStatus.SENT
        assert draft_announcement.sent_at is not None
        assert draft_announcement.recipient_count == 1

    def test_send_announcement_creates_notifications(
        self,
        draft_announcement: Announcement,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that sending creates notifications for recipients."""
        # Arrange
        members = []
        for i in range(2):
            member = revel_user_factory(username=f"member_{i}")
            OrganizationMember.objects.create(
                organization=org,
                user=member,
                status=OrganizationMember.MembershipStatus.ACTIVE,
            )
            members.append(member)

        # Act
        announcement_service.send_announcement(draft_announcement)

        # Assert
        for member in members:
            notification = Notification.objects.filter(
                user=member,
                notification_type=NotificationType.ORG_ANNOUNCEMENT,
            ).first()
            assert notification is not None
            assert notification.context["announcement_id"] == str(draft_announcement.id)

    def test_send_announcement_with_no_recipients_succeeds(
        self,
        draft_announcement: Announcement,
    ) -> None:
        """Test that sending with no recipients succeeds."""
        # Act
        result = announcement_service.send_announcement(draft_announcement)

        # Assert
        assert result == 0
        draft_announcement.refresh_from_db()
        assert draft_announcement.status == Announcement.AnnouncementStatus.SENT
        assert draft_announcement.recipient_count == 0

    def test_send_announcement_already_sent_raises_error(
        self,
        sent_announcement: Announcement,
    ) -> None:
        """Test that sending an already sent announcement raises error."""
        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            announcement_service.send_announcement(sent_announcement)

        assert "Only draft announcements can be sent" in str(exc_info.value)

    def test_send_announcement_includes_event_context(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that event-targeted announcements include event in context."""
        # Arrange
        event = Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )
        free_tier = TicketTier.objects.create(
            event=event,
            name="Free",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

        attendee = revel_user_factory(username="attendee")
        Ticket.objects.create(
            event=event,
            user=attendee,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Attendee",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        announcement_service.send_announcement(announcement)

        # Assert
        notification = Notification.objects.get(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
        )
        assert notification.context["event_id"] == str(event.id)
        assert notification.context["event_name"] == event.name
