"""Tests for the organization service."""

from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import create_token
from accounts.models import RevelUser
from events import schema
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError
from events.models import (
    MembershipTier,
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
        tier = MembershipTier.objects.create(organization=organization, name="Gold")
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        member = organization_service.add_member(organization, nonmember_user, tier)
        assert member is not None
        assert member.tier == tier
        assert OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_add_member_already_exists_fails(self, organization_membership: OrganizationMember) -> None:
        """Test that adding an existing member raises an error."""
        tier = MembershipTier.objects.create(organization=organization_membership.organization, name="Silver")
        with pytest.raises(AlreadyMemberError):
            organization_service.add_member(organization_membership.organization, organization_membership.user, tier)

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


@pytest.mark.django_db(transaction=True)
class TestCreateOrganization:
    """Tests for the create_organization function."""

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_success(self, mock_send_email: MagicMock, nonmember_user: RevelUser) -> None:
        """Test that an organization is created successfully."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.save()

        # Act
        organization = organization_service.create_organization(
            owner=nonmember_user,
            name="Test Org",
            contact_email="contact@example.com",
            description="Test description",
        )

        # Assert
        assert organization.name == "Test Org"
        assert organization.owner == nonmember_user
        assert organization.description == "Test description"
        assert organization.contact_email == "contact@example.com"
        assert organization.contact_email_verified is False
        assert organization.visibility == Organization.Visibility.STAFF_ONLY

        # Check that verification email is sent
        assert mock_send_email.called
        call_args = mock_send_email.call_args[1]
        assert call_args["email"] == "contact@example.com"
        assert call_args["organization_name"] == "Test Org"
        assert "token" in call_args

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_with_owner_email_auto_verifies(
        self, mock_send_email: MagicMock, nonmember_user: RevelUser
    ) -> None:
        """Test that contact email is auto-verified when it matches owner's verified email."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.email = "owner@example.com"
        nonmember_user.save()

        # Act
        organization = organization_service.create_organization(
            owner=nonmember_user,
            name="Test Org",
            contact_email="owner@example.com",
            description="Test description",
        )

        # Assert
        assert organization.contact_email == "owner@example.com"
        assert organization.contact_email_verified is True

        # Check that no verification email is sent when auto-verified
        assert not mock_send_email.called

    def test_create_organization_user_already_owns_one_fails(self, organization: Organization) -> None:
        """Test that a user cannot create a second organization."""
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.create_organization(
                owner=organization.owner,
                name="Second Org",
                contact_email="contact@example.com",
            )
        assert exc_info.value.status_code == 400
        assert "already own an organization" in str(exc_info.value)

    def test_create_organization_with_unverified_owner_email(self, nonmember_user: RevelUser) -> None:
        """Test that contact email is not auto-verified when owner's email is unverified."""
        # Arrange
        nonmember_user.email_verified = False
        nonmember_user.email = "owner@example.com"
        nonmember_user.save()

        # Act
        organization = organization_service.create_organization(
            owner=nonmember_user,
            name="Test Org",
            contact_email="owner@example.com",
        )

        # Assert
        assert organization.contact_email == "owner@example.com"
        assert organization.contact_email_verified is False


@pytest.mark.django_db(transaction=True)
class TestUpdateContactEmail:
    """Tests for the update_contact_email function."""

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_update_contact_email_success(
        self, mock_send_email: MagicMock, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test updating contact email successfully."""
        # Act
        token = organization_service.update_contact_email(
            organization=organization,
            new_email="newemail@example.com",
            requester=organization_owner_user,
        )

        # Assert
        organization.refresh_from_db()
        assert organization.contact_email == "newemail@example.com"
        assert organization.contact_email_verified is False
        assert token != ""
        assert mock_send_email.called
        mock_send_email.assert_called_once_with(
            email="newemail@example.com",
            token=token,
            organization_name=organization.name,
            organization_slug=organization.slug,
        )

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_update_contact_email_auto_verifies_with_user_email(
        self, mock_send_email: MagicMock, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that contact email is auto-verified when it matches requester's verified email."""
        # Arrange
        organization_owner_user.email_verified = True
        organization_owner_user.email = "owner@example.com"
        organization_owner_user.save()

        # Act
        token = organization_service.update_contact_email(
            organization=organization,
            new_email="owner@example.com",
            requester=organization_owner_user,
        )

        # Assert
        organization.refresh_from_db()
        assert organization.contact_email == "owner@example.com"
        assert organization.contact_email_verified is True
        assert token == ""  # No token needed when auto-verified

        # Check that no email is sent when auto-verified
        assert not mock_send_email.called

    def test_update_contact_email_same_email_fails(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that updating to the same email fails."""
        # Arrange
        organization.contact_email = "existing@example.com"
        organization.save()

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.update_contact_email(
                organization=organization,
                new_email="existing@example.com",
                requester=organization_owner_user,
            )
        assert exc_info.value.status_code == 400
        assert "already the contact email" in str(exc_info.value)


@pytest.mark.django_db
class TestVerifyContactEmail:
    """Tests for the verify_contact_email function."""

    def test_verify_contact_email_success(self, organization: Organization, organization_owner_user: RevelUser) -> None:
        """Test verifying contact email with valid token."""
        # Arrange
        organization.contact_email = "test@example.com"
        organization.contact_email_verified = False
        organization.save()

        # Create a valid token
        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=organization.id,
            user_id=organization_owner_user.id,
            email="test@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        # Act
        verified_org = organization_service.verify_contact_email(token)

        # Assert
        assert verified_org.contact_email_verified is True
        assert verified_org.id == organization.id

    def test_verify_contact_email_wrong_email_fails(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that verification fails when email has changed."""
        # Arrange
        organization.contact_email = "current@example.com"
        organization.contact_email_verified = False
        organization.save()

        # Create a token for a different email
        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=organization.id,
            user_id=organization_owner_user.id,
            email="old@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.verify_contact_email(token)
        assert exc_info.value.status_code == 400
        assert "different email address" in str(exc_info.value)

    def test_verify_contact_email_invalid_organization_fails(self, organization_owner_user: RevelUser) -> None:
        """Test that verification fails for non-existent organization."""
        # Arrange - Create a token for non-existent organization
        from uuid import uuid4

        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=uuid4(),  # Non-existent ID
            user_id=organization_owner_user.id,
            email="test@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.verify_contact_email(token)
        assert exc_info.value.status_code == 400
        assert "Organization not found" in str(exc_info.value)

    def test_verify_contact_email_blacklisted_token_fails(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that verification fails with blacklisted token."""
        # Arrange
        organization.contact_email = "test@example.com"
        organization.contact_email_verified = False
        organization.save()

        # Create and blacklist a token
        verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
            organization_id=organization.id,
            user_id=organization_owner_user.id,
            email="test@example.com",
            exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        )
        token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
        blacklist_token(token)

        # Act & Assert
        with pytest.raises(HttpError):
            organization_service.verify_contact_email(token)
