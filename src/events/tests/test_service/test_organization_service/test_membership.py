"""Tests for the membership and staff side of organization_service.

Covers membership requests (create/approve/reject), invitation claiming, and
member/staff management. Organization creation and contact-email flows live in
test_lifecycle.py; token services in test_tokens.py.
"""

import typing as t
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import (
    AlreadyMemberError,
    PendingMembershipRequestExistsError,
)
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


@contextmanager
def _force_first_lookup_miss(manager: t.Any) -> t.Iterator[None]:
    """Make ``manager.filter(...)`` miss on its first call, then behave normally.

    Simulates the concurrent double-submit the partial unique constraint now
    catches: the racing row is already committed by the time our INSERT lands,
    but our own lookup ran too early to see it.
    """
    call_count = 0
    real_filter = manager.filter

    def fake_filter(*args: t.Any, **kwargs: t.Any) -> t.Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return manager.none()
        return real_filter(*args, **kwargs)

    with patch.object(manager, "filter", side_effect=fake_filter):
        yield


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

    def test_create_membership_request_blacklisted_user_fails(
        self, organization: Organization, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """Test that a blacklisted user cannot create a membership request."""
        from events.models import Blacklist

        # Arrange - blacklist the user by direct FK match
        Blacklist.objects.create(
            organization=organization,
            user=nonmember_user,
            email=nonmember_user.email,
            created_by=organization_owner_user,
            reason="Test blacklist",
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.create_membership_request(organization, nonmember_user)
        assert exc_info.value.status_code == 403

    def test_create_membership_request_blacklisted_by_email_fails(
        self, organization: Organization, nonmember_user: RevelUser, organization_owner_user: RevelUser
    ) -> None:
        """Test that a user blacklisted by email (without FK) cannot create a membership request."""
        from events.models import Blacklist

        # Arrange - blacklist by email only (no user FK)
        Blacklist.objects.create(
            organization=organization,
            email=nonmember_user.email,
            created_by=organization_owner_user,
            reason="Test blacklist by email",
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            organization_service.create_membership_request(organization, nonmember_user)
        assert exc_info.value.status_code == 403

    def test_create_membership_request_lost_race_raises_domain_error(
        self, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """A concurrent double-submit must still yield the 409 domain error, not a 500.

        ``unique_pending_application_per_user_org_tier`` rejects the second insert.
        This is the ValidationError arm: ``TimeStampedModel.save`` runs ``full_clean``,
        so ``validate_constraints`` sees the committed racing row before the INSERT.
        """
        existing = OrganizationMembershipRequest.objects.create(organization=organization, user=nonmember_user)

        with _force_first_lookup_miss(OrganizationMembershipRequest.objects):
            with pytest.raises(PendingMembershipRequestExistsError):
                organization_service.create_membership_request(organization, nonmember_user)

        surviving = OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user)
        assert [row.pk for row in surviving] == [existing.pk]

    def test_create_membership_request_lost_db_level_race_raises_domain_error(
        self, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """The IntegrityError arm: ``full_clean`` disabled, so the unique index rejects the INSERT.

        Also pins that the ambient transaction survives — the helper's savepoint is
        what keeps the recovery re-fetch from raising ``TransactionManagementError``.
        """
        existing = OrganizationMembershipRequest.objects.create(organization=organization, user=nonmember_user)

        with (
            _force_first_lookup_miss(OrganizationMembershipRequest.objects),
            patch.object(OrganizationMembershipRequest, "full_clean", return_value=None),
        ):
            with pytest.raises(PendingMembershipRequestExistsError):
                organization_service.create_membership_request(organization, nonmember_user)

        surviving = OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user)
        assert [row.pk for row in surviving] == [existing.pk]


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
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.COMPLETED
        assert organization_membership_request.decided_by == organization_staff_user

    def test_approve_survives_lost_member_creation_race(
        self, organization_membership_request: OrganizationMembershipRequest, organization_staff_user: RevelUser
    ) -> None:
        """A membership row committed concurrently must not 500 the approval.

        ``(organization, user)`` is unique and ``TimeStampedModel.save`` runs
        ``full_clean``, so a row created between the guards and our INSERT surfaces
        as ``ValidationError`` — which Django's ``update_or_create`` does not absorb.
        """
        tier = MembershipTier.objects.get(
            organization=organization_membership_request.organization, name="General membership"
        )
        existing = OrganizationMember.objects.create(
            organization=organization_membership_request.organization,
            user=organization_membership_request.user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        with patch.object(
            OrganizationMember.objects,
            "update_or_create",
            side_effect=ValidationError("Organization member with this Organization and User already exists."),
        ):
            organization_service.approve_membership_request(
                organization_membership_request, organization_staff_user, tier
            )

        existing.refresh_from_db()
        assert existing.tier == tier
        assert existing.status == OrganizationMember.MembershipStatus.ACTIVE
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.COMPLETED


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

    def test_claim_invitation_grants_both_staff_and_membership(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        """A token granting both staff and membership must apply both, not just staff."""
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        token = OrganizationToken.objects.create(
            organization=organization,
            issuer=organization_owner_user,
            grants_staff_status=True,
            grants_membership=True,
            membership_tier=default_tier,
        )

        # Act
        claimed_org = organization_service.claim_invitation(nonmember_user, token.id)

        # Assert - both grants applied, and the use is consumed exactly once
        assert claimed_org == organization
        assert OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()
        member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
        assert member.tier == default_tier
        token.refresh_from_db()
        assert token.uses == 1


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

    def test_add_member_lost_race_raises_domain_error(self, organization_membership: OrganizationMember) -> None:
        """A concurrent membership create must surface AlreadyMemberError, not a 500.

        ``(organization, user)`` is unique and ``TimeStampedModel.save`` runs
        ``full_clean``, so the bare ``create()`` used to blow up with
        ``ValidationError`` when the pre-check ran too early to see the winner.
        """
        organization = organization_membership.organization
        tier = MembershipTier.objects.create(organization=organization, name="Bronze")

        with _force_first_lookup_miss(OrganizationMember.objects):
            with pytest.raises(AlreadyMemberError):
                organization_service.add_member(organization, organization_membership.user, tier)

        surviving = OrganizationMember.objects.filter(organization=organization, user=organization_membership.user)
        assert [row.pk for row in surviving] == [organization_membership.pk]

    def test_add_member_lost_db_level_race_raises_domain_error(
        self, organization_membership: OrganizationMember
    ) -> None:
        """The IntegrityError arm: ``full_clean`` disabled, so the unique index rejects the INSERT."""
        organization = organization_membership.organization
        tier = MembershipTier.objects.create(organization=organization, name="Copper")

        with (
            _force_first_lookup_miss(OrganizationMember.objects),
            patch.object(OrganizationMember, "full_clean", return_value=None),
        ):
            with pytest.raises(AlreadyMemberError):
                organization_service.add_member(organization, organization_membership.user, tier)

        surviving = OrganizationMember.objects.filter(organization=organization, user=organization_membership.user)
        assert [row.pk for row in surviving] == [organization_membership.pk]

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
