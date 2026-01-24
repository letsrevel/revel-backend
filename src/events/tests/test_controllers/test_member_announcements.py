"""Tests for organization member announcements endpoint.

This module tests the GET /organizations/{slug}/member-announcements endpoint
which allows organization members to view announcements targeted at them.
"""

from datetime import timedelta

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestMemberAnnouncementsEndpoint:
    """Tests for GET /organizations/{slug}/member-announcements endpoint."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization with members-only visibility."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
            visibility=Organization.Visibility.MEMBERS_ONLY,
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """VIP membership tier."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )

    @pytest.fixture
    def member(
        self,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def member_client(self, member: RevelUser) -> Client:
        """Authenticated client for member."""
        refresh = RefreshToken.for_user(member)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def vip_member(
        self,
        org: Organization,
        membership_tier: MembershipTier,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """VIP tier member."""
        user = revel_user_factory(username="vip_member")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            tier=membership_tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def vip_member_client(self, vip_member: RevelUser) -> Client:
        """Authenticated client for VIP member."""
        refresh = RefreshToken.for_user(vip_member)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def staff_user(
        self,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """Staff member."""
        user = revel_user_factory(username="staff")
        OrganizationStaff.objects.create(
            organization=org,
            user=user,
            permissions=PermissionsSchema(default=PermissionMap()).model_dump(mode="json"),
        )
        return user

    @pytest.fixture
    def staff_client(self, staff_user: RevelUser) -> Client:
        """Authenticated client for staff member."""
        refresh = RefreshToken.for_user(staff_user)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def non_member(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """User with no relationship to the organization."""
        return revel_user_factory(username="non_member")

    @pytest.fixture
    def non_member_client(self, non_member: RevelUser) -> Client:
        """Authenticated client for non-member."""
        refresh = RefreshToken.for_user(non_member)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    def test_member_sees_all_members_announcement_with_notification(
        self,
        member_client: Client,
        member: RevelUser,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that member can see announcements they received notifications for.

        When a member received a notification for a target_all_members announcement,
        they should be able to see it.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Important update for all members!",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create notification for the member
        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(announcement.id)},
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)
        assert data[0]["title"] == "Members Announcement"

    def test_new_member_sees_announcement_with_past_visibility(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new members can see announcements with past_visibility enabled.

        If past_visibility is True, new members who joined after the announcement
        was sent should still see it.
        """
        # Arrange - Create announcement with past_visibility
        announcement = Announcement.objects.create(
            organization=org,
            title="Past Visible Announcement",
            body="Body",
            target_all_members=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create new member AFTER announcement was sent (no notification)
        new_member = revel_user_factory(username="new_member")
        OrganizationMember.objects.create(
            organization=org,
            user=new_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        refresh = RefreshToken.for_user(new_member)
        new_member_client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = new_member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)

    def test_new_member_does_not_see_announcement_without_past_visibility(
        self,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new members cannot see announcements without past_visibility.

        If past_visibility is False, new members who didn't receive the notification
        should not see the announcement.
        """
        # Arrange - Create announcement WITHOUT past_visibility
        Announcement.objects.create(
            organization=org,
            title="No Past Visibility",
            body="Body",
            target_all_members=True,
            past_visibility=False,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create new member AFTER announcement was sent (no notification)
        new_member = revel_user_factory(username="new_member")
        OrganizationMember.objects.create(
            organization=org,
            user=new_member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        refresh = RefreshToken.for_user(new_member)
        new_member_client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = new_member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_staff_sees_staff_only_announcement(
        self,
        staff_client: Client,
        staff_user: RevelUser,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that staff can see staff-only announcements.

        Staff members should see announcements targeted at staff.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Staff Announcement",
            body="For staff only",
            target_staff_only=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = staff_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)

    def test_member_does_not_see_staff_only_announcement(
        self,
        member_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that regular members cannot see staff-only announcements.

        Staff-only announcements should be hidden from regular members.
        """
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Staff Announcement",
            body="For staff only",
            target_staff_only=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_vip_member_sees_tier_announcement(
        self,
        vip_member_client: Client,
        vip_member: RevelUser,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
    ) -> None:
        """Test that VIP members can see tier-targeted announcements.

        Members of the target tier should see the announcement.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="VIP Announcement",
            body="For VIP members",
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        announcement.target_tiers.add(membership_tier)

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = vip_member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)

    def test_non_vip_member_does_not_see_tier_announcement(
        self,
        member_client: Client,
        org: Organization,
        org_owner: RevelUser,
        membership_tier: MembershipTier,
    ) -> None:
        """Test that non-VIP members cannot see VIP-only announcements.

        Members not in the target tier should not see the announcement.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="VIP Announcement",
            body="For VIP members",
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        announcement.target_tiers.add(membership_tier)

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_excludes_event_announcements(
        self,
        member_client: Client,
        member: RevelUser,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that event-targeted announcements are excluded.

        Member announcements endpoint should only show org-level announcements,
        not event-specific ones.
        """
        # Arrange
        from events.models import Event

        event = Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            start=timezone.now() + timedelta(days=7),
        )

        # Event announcement
        event_announcement = Announcement.objects.create(
            organization=org,
            event=event,  # Event-targeted
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(event_announcement.id)},
        )

        # Member announcement
        member_announcement = Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(member_announcement.id)},
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(member_announcement.id)

    def test_draft_announcements_not_visible(
        self,
        member_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that draft announcements are not visible.

        Only SENT announcements should appear in the list.
        """
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_announcements_ordered_by_sent_date_newest_first(
        self,
        member_client: Client,
        member: RevelUser,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that announcements are ordered by sent_at descending.

        Newest announcements should appear first.
        """
        # Arrange
        old_announcement = Announcement.objects.create(
            organization=org,
            title="Old Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now() - timedelta(days=2),
        )
        new_announcement = Announcement.objects.create(
            organization=org,
            title="New Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create notifications for both
        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(old_announcement.id)},
        )
        Notification.objects.create(
            user=member,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(new_announcement.id)},
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == str(new_announcement.id)  # Newest first
        assert data[1]["id"] == str(old_announcement.id)

    def test_unauthenticated_user_gets_401(
        self,
        client: Client,
        org: Organization,
    ) -> None:
        """Test that unauthenticated users get 401."""
        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = client.get(url)

        # Assert
        assert response.status_code == 401

    def test_non_member_cannot_access_private_org_announcements(
        self,
        non_member_client: Client,
        org: Organization,
    ) -> None:
        """Test that non-members cannot access announcements for non-visible orgs.

        Users who cannot see the organization should get 404.
        """
        # Arrange - Org is already members-only
        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = non_member_client.get(url)

        # Assert
        assert response.status_code == 404

    def test_nonexistent_org_returns_404(
        self,
        member_client: Client,
    ) -> None:
        """Test that non-existent organization returns 404."""
        url = reverse("api:list_member_announcements", kwargs={"slug": "nonexistent-org"})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 404

    def test_response_includes_organization_name(
        self,
        member_client: Client,
        member: RevelUser,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that response includes organization name for display."""
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

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data[0]["organization_name"] == org.name

    def test_public_org_allows_non_member_to_see_announcements(
        self,
        non_member_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that public org announcements are visible to any authenticated user.

        Public organizations should allow any authenticated user to see announcements,
        but they still need to be eligible based on targeting.
        """
        # Arrange - Make org public
        org.visibility = Organization.Visibility.PUBLIC
        org.save()

        # Non-member won't be eligible for target_all_members
        Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_member_announcements", kwargs={"slug": org.slug})

        # Act
        response = non_member_client.get(url)

        # Assert - Can access endpoint but sees no announcements (not a member)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0
