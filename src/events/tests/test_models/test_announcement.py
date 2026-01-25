"""Tests for the Announcement model.

This module tests the Announcement model including queryset methods,
model properties, and string representation.
"""

import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Announcement, Event, MembershipTier, Organization

pytestmark = pytest.mark.django_db


class TestAnnouncementModel:
    """Tests for the Announcement model."""

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
            start=timezone.now(),
        )

    @pytest.fixture
    def draft_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Draft announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Test Draft Announcement",
            body="This is a draft announcement body.",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )

    @pytest.fixture
    def sent_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Sent announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Test Sent Announcement",
            body="This is a sent announcement body.",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
            recipient_count=10,
        )

    def test_announcement_str_representation(
        self,
        draft_announcement: Announcement,
    ) -> None:
        """Test that announcement string representation is correct.

        The string representation should include the title and organization name.
        """
        # Act
        result = str(draft_announcement)

        # Assert
        assert draft_announcement.title in result
        assert draft_announcement.organization.name in result

    def test_announcement_default_status_is_draft(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that announcements are created with DRAFT status by default.

        When creating an announcement without specifying status,
        it should default to DRAFT.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            title="New Announcement",
            body="Body content",
            target_all_members=True,
            created_by=org_owner,
        )

        # Assert
        assert announcement.status == Announcement.AnnouncementStatus.DRAFT

    def test_announcement_default_past_visibility_is_true(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that announcements default to past_visibility=True.

        This allows new attendees/members to see the announcement after it was sent.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            title="New Announcement",
            body="Body content",
            target_all_members=True,
            created_by=org_owner,
        )

        # Assert
        assert announcement.past_visibility is True


class TestAnnouncementQuerySet:
    """Tests for AnnouncementQuerySet methods."""

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
            start=timezone.now(),
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """Membership tier fixture."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )

    @pytest.fixture
    def draft_announcement(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """Draft announcement fixture."""
        return Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Draft body",
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
            body="Sent body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
        )

    def test_drafts_returns_only_draft_announcements(
        self,
        draft_announcement: Announcement,
        sent_announcement: Announcement,
    ) -> None:
        """Test that drafts() returns only DRAFT status announcements.

        The queryset should filter out sent announcements.
        """
        # Act
        drafts = Announcement.objects.drafts()

        # Assert
        assert draft_announcement in drafts
        assert sent_announcement not in drafts

    def test_sent_returns_only_sent_announcements(
        self,
        draft_announcement: Announcement,
        sent_announcement: Announcement,
    ) -> None:
        """Test that sent() returns only SENT status announcements.

        The queryset should filter out draft announcements.
        """
        # Act
        sent = Announcement.objects.sent()

        # Assert
        assert sent_announcement in sent
        assert draft_announcement not in sent

    def test_with_organization_prefetches_organization(
        self,
        draft_announcement: Announcement,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Test that with_organization() prefetches organization data.

        Accessing organization should not require additional queries.
        """
        # Act
        announcement = Announcement.objects.with_organization().get(id=draft_announcement.id)

        # Assert - accessing organization should not cause additional query
        with django_assert_num_queries(0):
            _ = announcement.organization.name

    def test_with_event_prefetches_event(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Test that with_event() prefetches event data.

        Accessing event should not require additional queries.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        # Act
        fetched = Announcement.objects.with_event().get(id=announcement.id)

        # Assert - accessing event should not cause additional query
        with django_assert_num_queries(0):
            _ = fetched.event.name if fetched.event else None

    def test_with_created_by_prefetches_creator(
        self,
        draft_announcement: Announcement,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Test that with_created_by() prefetches creator data.

        Accessing created_by should not require additional queries.
        """
        # Act
        announcement = Announcement.objects.with_created_by().get(id=draft_announcement.id)

        # Assert - accessing created_by should not cause additional query
        with django_assert_num_queries(0):
            _ = announcement.created_by.email if announcement.created_by else None

    def test_with_target_tiers_prefetches_tiers(
        self,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Test that with_target_tiers() prefetches target tier data.

        Accessing target_tiers should not require additional queries.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Tier Announcement",
            body="Body",
            created_by=org_owner,
        )
        announcement.target_tiers.add(membership_tier)

        # Act
        fetched = Announcement.objects.with_target_tiers().get(id=announcement.id)

        # Assert - accessing target_tiers should not cause additional query
        with django_assert_num_queries(0):
            _ = list(fetched.target_tiers.all())

    def test_full_prefetches_all_related_data(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        membership_tier: MembershipTier,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Test that full() prefetches all related data.

        Accessing all related fields should not require additional queries.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Full Announcement",
            body="Body",
            created_by=org_owner,
        )
        announcement.target_tiers.add(membership_tier)

        # Act - single query to get announcement with all relations
        fetched = Announcement.objects.full().get(id=announcement.id)

        # Assert - accessing all related fields should not cause additional queries
        with django_assert_num_queries(0):
            _ = fetched.organization.name
            _ = fetched.event.name if fetched.event else None
            _ = fetched.created_by.email if fetched.created_by else None
            _ = list(fetched.target_tiers.all())


class TestAnnouncementTargeting:
    """Tests for announcement targeting options."""

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
            start=timezone.now(),
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """Membership tier fixture."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )

    def test_announcement_with_event_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
    ) -> None:
        """Test creating announcement targeting event attendees.

        Event-targeted announcements should have event set and other
        targeting options disabled.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="For event attendees",
            created_by=org_owner,
        )

        # Assert
        assert announcement.event == event
        assert announcement.target_all_members is False
        assert announcement.target_staff_only is False

    def test_announcement_with_all_members_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test creating announcement targeting all organization members.

        All-members announcements should have target_all_members=True
        and no event.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="For all members",
            target_all_members=True,
            created_by=org_owner,
        )

        # Assert
        assert announcement.event is None
        assert announcement.target_all_members is True
        assert announcement.target_staff_only is False

    def test_announcement_with_tier_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
    ) -> None:
        """Test creating announcement targeting specific membership tiers.

        Tier-targeted announcements should have the tiers set via M2M.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            title="VIP Announcement",
            body="For VIP members",
            created_by=org_owner,
        )
        announcement.target_tiers.add(membership_tier)

        # Assert
        assert announcement.event is None
        assert announcement.target_all_members is False
        assert membership_tier in announcement.target_tiers.all()

    def test_announcement_with_staff_only_targeting(
        self,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test creating announcement targeting only staff members.

        Staff-only announcements should have target_staff_only=True.
        """
        # Arrange & Act
        announcement = Announcement.objects.create(
            organization=org,
            title="Staff Announcement",
            body="For staff only",
            target_staff_only=True,
            created_by=org_owner,
        )

        # Assert
        assert announcement.event is None
        assert announcement.target_all_members is False
        assert announcement.target_staff_only is True
