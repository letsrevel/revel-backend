"""Tests for the organization-token services in organization_service."""

import pytest

from accounts.models import RevelUser
from events import schema
from events.exceptions import (
    OrganizationTokenGrantInvariantError,
    OrganizationTokenMembershipTierRequiredError,
    OrganizationTokenStaffGrantForbidden,
)
from events.models import (
    MembershipTier,
    Organization,
    OrganizationToken,
)
from events.service import organization_service


@pytest.mark.django_db
class TestCreateOrganizationTokenValidation:
    """Tests for M-02: organization tokens must grant at least one type of access."""

    def test_create_token_with_grants_membership_succeeds(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Token with grants_membership=True can be created via service."""
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        token = organization_service.create_organization_token(
            organization=organization,
            issuer=organization_owner_user,
            grants_membership=True,
            grants_staff_status=False,
            membership_tier=default_tier,
        )
        assert token.grants_membership is True
        assert token.grants_staff_status is False

    def test_create_token_with_grants_staff_status_succeeds(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Token with grants_staff_status=True can be created via service."""
        token = organization_service.create_organization_token(
            organization=organization,
            issuer=organization_owner_user,
            grants_membership=False,
            grants_staff_status=True,
        )
        assert token.grants_membership is False
        assert token.grants_staff_status is True

    def test_create_token_with_both_grants_succeeds(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Token with both grants enabled can be created via service."""
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        token = organization_service.create_organization_token(
            organization=organization,
            issuer=organization_owner_user,
            grants_membership=True,
            grants_staff_status=True,
            membership_tier=default_tier,
        )
        assert token.grants_membership is True
        assert token.grants_staff_status is True

    def test_create_token_with_no_grants_raises_value_error(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Token with both grants disabled raises ValueError in service."""
        with pytest.raises(ValueError, match="At least one of grants_membership or grants_staff_status must be True"):
            organization_service.create_organization_token(
                organization=organization,
                issuer=organization_owner_user,
                grants_membership=False,
                grants_staff_status=False,
            )


@pytest.mark.django_db
class TestCreateOrganizationTokenFromPayload:
    """Tests for ``organization_service.create_organization_token_from_payload``."""

    def test_owner_can_create_staff_granting_token(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        payload = schema.OrganizationTokenCreateSchema(
            name="Staff Token",
            grants_membership=True,
            grants_staff_status=True,
            membership_tier_id=default_tier.id,
        )

        token = organization_service.create_organization_token_from_payload(
            organization=organization, requested_by=organization_owner_user, payload=payload
        )

        assert token.grants_staff_status is True
        assert token.membership_tier_id == default_tier.id

    def test_non_owner_cannot_create_staff_granting_token(
        self, organization: Organization, organization_staff_user: RevelUser
    ) -> None:
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        payload = schema.OrganizationTokenCreateSchema(
            name="Staff Token",
            grants_membership=True,
            grants_staff_status=True,
            membership_tier_id=default_tier.id,
        )

        with pytest.raises(OrganizationTokenStaffGrantForbidden):
            organization_service.create_organization_token_from_payload(
                organization=organization, requested_by=organization_staff_user, payload=payload
            )

    def test_non_owner_can_create_membership_only_token(
        self, organization: Organization, organization_staff_user: RevelUser
    ) -> None:
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        payload = schema.OrganizationTokenCreateSchema(
            name="Member Token",
            grants_membership=True,
            grants_staff_status=False,
            membership_tier_id=default_tier.id,
        )

        token = organization_service.create_organization_token_from_payload(
            organization=organization, requested_by=organization_staff_user, payload=payload
        )

        assert token.grants_membership is True
        assert token.grants_staff_status is False


@pytest.mark.django_db
class TestUpdateOrganizationTokenService:
    """Tests for the high-level ``organization_service.update_organization_token``."""

    def test_grant_invariant_violation_raises(
        self, organization: Organization, staff_organization_token: OrganizationToken
    ) -> None:
        # The staff token already has grants_membership=False. A partial update
        # that flips grants_staff_status=False would leave both False, which the
        # schema's "both explicitly set" validator misses (only one field is in
        # model_fields_set). The service-side check catches it as defense-in-depth.
        payload = schema.OrganizationTokenUpdateSchema(grants_staff_status=False)

        with pytest.raises(OrganizationTokenGrantInvariantError):
            organization_service.update_organization_token(
                staff_organization_token, requested_by=organization.owner, payload=payload
            )

        staff_organization_token.refresh_from_db()
        assert staff_organization_token.grants_staff_status is True

    def test_non_owner_cannot_promote_token_to_staff(
        self,
        organization: Organization,
        organization_token: OrganizationToken,
        organization_staff_user: RevelUser,
    ) -> None:
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        payload = schema.OrganizationTokenUpdateSchema(
            grants_staff_status=True, grants_membership=True, membership_tier_id=default_tier.id
        )

        with pytest.raises(OrganizationTokenStaffGrantForbidden):
            organization_service.update_organization_token(
                organization_token, requested_by=organization_staff_user, payload=payload
            )

        organization_token.refresh_from_db()
        assert organization_token.grants_staff_status is False

    def test_clearing_membership_tier_while_grants_membership_raises(
        self, organization: Organization, organization_token: OrganizationToken
    ) -> None:
        # The token has grants_membership=True with a tier. A partial update
        # that only sets membership_tier_id=None slips past the schema validator
        # (grants_membership is not in model_fields_set), but would leave the
        # token in an inconsistent state that OrganizationToken.clean() rejects
        # at full_clean() time — surfacing as a 500. The service-side check
        # raises a structured exception the controller maps to 422.
        payload = schema.OrganizationTokenUpdateSchema(membership_tier_id=None)

        with pytest.raises(OrganizationTokenMembershipTierRequiredError):
            organization_service.update_organization_token(
                organization_token, requested_by=organization.owner, payload=payload
            )

        organization_token.refresh_from_db()
        assert organization_token.membership_tier_id is not None

    def test_non_owner_cannot_touch_existing_staff_token(
        self,
        organization: Organization,
        staff_organization_token: OrganizationToken,
        organization_staff_user: RevelUser,
    ) -> None:
        payload = schema.OrganizationTokenUpdateSchema(name="renamed")

        with pytest.raises(OrganizationTokenStaffGrantForbidden):
            organization_service.update_organization_token(
                staff_organization_token, requested_by=organization_staff_user, payload=payload
            )

    def test_owner_can_update_membership_tier(
        self,
        organization: Organization,
        organization_token: OrganizationToken,
        organization_owner_user: RevelUser,
    ) -> None:
        new_tier = MembershipTier.objects.create(organization=organization, name="VIP")
        payload = schema.OrganizationTokenUpdateSchema(membership_tier_id=new_tier.id)

        updated = organization_service.update_organization_token(
            organization_token, requested_by=organization_owner_user, payload=payload
        )

        assert updated.membership_tier_id == new_tier.id


@pytest.mark.django_db
class TestDeleteOrganizationTokenService:
    """Tests for the high-level ``organization_service.delete_organization_token``."""

    def test_owner_can_delete_staff_token(
        self,
        organization_owner_user: RevelUser,
        staff_organization_token: OrganizationToken,
    ) -> None:
        token_id = staff_organization_token.id
        organization_service.delete_organization_token(staff_organization_token, requested_by=organization_owner_user)
        assert not OrganizationToken.objects.filter(pk=token_id).exists()

    def test_non_owner_cannot_delete_staff_token(
        self,
        staff_organization_token: OrganizationToken,
        organization_staff_user: RevelUser,
    ) -> None:
        with pytest.raises(OrganizationTokenStaffGrantForbidden):
            organization_service.delete_organization_token(
                staff_organization_token, requested_by=organization_staff_user
            )
        assert OrganizationToken.objects.filter(pk=staff_organization_token.id).exists()

    def test_non_owner_can_delete_membership_only_token(
        self,
        organization_token: OrganizationToken,
        organization_staff_user: RevelUser,
    ) -> None:
        token_id = organization_token.id
        organization_service.delete_organization_token(organization_token, requested_by=organization_staff_user)
        assert not OrganizationToken.objects.filter(pk=token_id).exists()
