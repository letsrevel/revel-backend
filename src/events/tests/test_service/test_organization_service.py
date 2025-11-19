"""Tests for the organization service."""

import pytest

from accounts.models import RevelUser
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError
from events.models import (
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
    PermissionMap,
    PermissionsSchema,
)
from events.service import organization_service


@pytest.fixture
def organization_token(organization: Organization, organization_owner_user: RevelUser) -> OrganizationToken:
    """An organization token that grants membership."""
    from events.models import MembershipTier

    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return OrganizationToken.objects.create(
        organization=organization, issuer=organization_owner_user, membership_tier=default_tier
    )


@pytest.fixture
def staff_organization_token(organization: Organization, organization_owner_user: RevelUser) -> OrganizationToken:
    """An organization token that grants staff permissions."""
    return OrganizationToken.objects.create(
        organization=organization, issuer=organization_owner_user, grants_staff_status=True, grants_membership=False
    )


@pytest.mark.django_db
class TestCreateMembershipRequest:
    """Tests for the create_membership_request function."""

    def test_create_membership_request_success(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test that a membership request is created successfully."""
        # Act
        request = organization_service.create_membership_request(organization, nonmember_user)

        # Assert
        assert OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()
        assert request.status == OrganizationMembershipRequest.Status.PENDING

    def test_create_membership_request_already_member_fails(self, organization_membership: OrganizationMember) -> None:
        """Test that a membership request is not created if the user is already a member."""
        # Act & Assert
        with pytest.raises(AlreadyMemberError):
            organization_service.create_membership_request(
                organization_membership.organization, organization_membership.user
            )

    def test_create_membership_request_pending_request_exists_fails(
        self, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test that a membership request is not created if a pending request already exists."""
        # Arrange
        OrganizationMembershipRequest.objects.create(organization=organization, user=nonmember_user)

        # Act & Assert
        with pytest.raises(PendingMembershipRequestExistsError):
            organization_service.create_membership_request(organization, nonmember_user)


@pytest.mark.django_db
class TestApproveMembershipRequest:
    """Tests for the approve_membership_request function."""

    def test_approve_membership_request_creates_member(
        self, organization_membership_request: OrganizationMembershipRequest, organization_staff_user: RevelUser
    ) -> None:
        """Test that a member is created when a request is approved."""
        # Arrange
        from events.models import MembershipTier

        tier = MembershipTier.objects.get(
            organization=organization_membership_request.organization, name="General membership"
        )

        assert not OrganizationMember.objects.filter(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        ).exists()

        # Act
        organization_service.approve_membership_request(organization_membership_request, organization_staff_user, tier)

        # Assert
        member = OrganizationMember.objects.get(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        )
        assert member is not None
        assert member.tier == tier
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.APPROVED
        assert organization_membership_request.decided_by == organization_staff_user


@pytest.mark.django_db
class TestRejectMembershipRequest:
    """Tests for the reject_membership_request function."""

    def test_reject_membership_request_does_not_create_member(
        self, organization_membership_request: OrganizationMembershipRequest, organization_staff_user: RevelUser
    ) -> None:
        """Test that a member is not created when a request is rejected."""
        # Arrange
        assert not OrganizationMember.objects.filter(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        ).exists()

        # Act
        organization_service.reject_membership_request(organization_membership_request, organization_staff_user)

        # Assert
        assert not OrganizationMember.objects.filter(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        ).exists()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.REJECTED
        assert organization_membership_request.decided_by == organization_staff_user


@pytest.mark.django_db
class TestClaimInvitation:
    """Tests for the claim_invitation function."""

    def test_claim_invitation_success(self, organization_token: OrganizationToken, nonmember_user: RevelUser) -> None:
        """Test that an invitation is claimed successfully."""
        # Act
        claimed_org = organization_service.claim_invitation(nonmember_user, organization_token.id)

        # Assert
        assert claimed_org == organization_token.organization
        assert OrganizationMember.objects.filter(
            organization=organization_token.organization, user=nonmember_user
        ).exists()
        assert not OrganizationStaff.objects.filter(
            organization=organization_token.organization, user=nonmember_user
        ).exists()

    def test_claim_invitation_staff_success(
        self, staff_organization_token: OrganizationToken, nonmember_user: RevelUser
    ) -> None:
        """Test that a staff invitation is claimed successfully."""
        # Act
        claimed_org = organization_service.claim_invitation(nonmember_user, staff_organization_token.id)

        # Assert
        assert claimed_org == staff_organization_token.organization
        assert OrganizationStaff.objects.filter(
            organization=staff_organization_token.organization, user=nonmember_user
        ).exists()
        assert not OrganizationMember.objects.filter(
            organization=staff_organization_token.organization, user=nonmember_user
        ).exists()


@pytest.mark.django_db
class TestMemberManagement:
    def test_add_member_success(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test that a user can be successfully added as a member."""
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        member = organization_service.add_member(organization, nonmember_user)
        assert member is not None
        assert OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_add_member_already_exists_fails(self, organization_membership: OrganizationMember) -> None:
        """Test that adding an existing member raises an error."""
        with pytest.raises(AlreadyMemberError):
            organization_service.add_member(organization_membership.organization, organization_membership.user)

    def test_remove_member_success(self, organization_membership: OrganizationMember) -> None:
        """Test that a member can be successfully removed."""
        organization = organization_membership.organization
        user = organization_membership.user
        assert OrganizationMember.objects.filter(organization=organization, user=user).exists()
        organization_service.remove_member(organization, user)
        assert not OrganizationMember.objects.filter(organization=organization, user=user).exists()


@pytest.mark.django_db
class TestStaffManagement:
    def test_add_staff_success(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test adding a staff member with default permissions."""
        assert not OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()
        staff = organization_service.add_staff(organization, nonmember_user)
        assert staff is not None
        assert staff.permissions is not None
        assert OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_add_staff_with_custom_permissions(self, organization: Organization, nonmember_user: RevelUser) -> None:
        """Test adding a staff member with custom permissions."""
        custom_perms = PermissionsSchema(default=PermissionMap(create_event=True, edit_event=False))
        staff = organization_service.add_staff(organization, nonmember_user, permissions=custom_perms)
        assert staff.permissions["default"]["create_event"] is True
        assert staff.permissions["default"]["edit_event"] is False

    def test_add_staff_already_exists_fails(self, staff_member: OrganizationStaff) -> None:
        """Test that adding an existing staff member raises an error."""
        with pytest.raises(AlreadyMemberError):
            organization_service.add_staff(staff_member.organization, staff_member.user)

    def test_remove_staff_success(self, staff_member: OrganizationStaff) -> None:
        """Test removing a staff member."""
        organization = staff_member.organization
        user = staff_member.user
        assert OrganizationStaff.objects.filter(organization=organization, user=user).exists()
        organization_service.remove_staff(organization, user)
        assert not OrganizationStaff.objects.filter(organization=organization, user=user).exists()

    def test_update_staff_permissions(self, staff_member: OrganizationStaff) -> None:
        """Test updating a staff member's permissions."""
        assert staff_member.has_permission("create_event") is False
        new_perms = PermissionsSchema(default=PermissionMap(create_event=True))

        updated_staff = organization_service.update_staff_permissions(staff_member, new_perms)
        updated_staff.refresh_from_db()

        assert updated_staff.has_permission("create_event") is True
