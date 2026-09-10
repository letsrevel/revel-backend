"""Tests for organization creation and contact-email verification in organization_service."""

from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import create_token
from accounts.models import RevelUser
from events import schema
from events.models import (
    Organization,
)
from events.service import organization_service


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
            name="Acme Collective",
            contact_email="contact@example.com",
            description="Test description",
        )

        # Assert
        assert organization.name == "Acme Collective"
        assert organization.owner == nonmember_user
        assert organization.description == "Test description"
        assert organization.contact_email == "contact@example.com"
        assert organization.contact_email_verified is False
        assert organization.visibility == Organization.Visibility.STAFF_ONLY

        # Check that verification email is sent
        assert mock_send_email.called
        call_args = mock_send_email.call_args[1]
        assert call_args["email"] == "contact@example.com"
        assert call_args["organization_name"] == "Acme Collective"
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
            name="Acme Collective",
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
                name="Pebble Society",
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
            name="Acme Collective",
            contact_email="owner@example.com",
        )

        # Assert
        assert organization.contact_email == "owner@example.com"
        assert organization.contact_email_verified is False

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_rejects_hardcoded_reserved_token(
        self, _mock_send_email: MagicMock, nonmember_user: RevelUser
    ) -> None:
        """Test that the service rejects names containing a hardcoded reserved token."""
        with pytest.raises(HttpError) as exc_info:
            organization_service.create_organization(
                owner=nonmember_user,
                name="Test Organization",
                contact_email="contact@example.com",
            )
        assert exc_info.value.status_code == 400
        assert "test" in str(exc_info.value).lower()

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_rejects_word_order_variant(
        self, _mock_send_email: MagicMock, nonmember_user: RevelUser
    ) -> None:
        """Test that the guard catches the reserved token regardless of position."""
        with pytest.raises(HttpError) as exc_info:
            organization_service.create_organization(
                owner=nonmember_user,
                name="Choir Test",
                contact_email="contact@example.com",
            )
        assert exc_info.value.status_code == 400

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_rejects_db_token(self, _mock_send_email: MagicMock, nonmember_user: RevelUser) -> None:
        """Test that the guard reads from the DB-backed reserved-token list."""
        from events.models import ReservedSlugToken
        from events.utils.reserved_slug_tokens import invalidate_reserved_tokens_cache

        ReservedSlugToken.objects.create(token="forbidden", reason="")
        invalidate_reserved_tokens_cache()
        with pytest.raises(HttpError):
            organization_service.create_organization(
                owner=nonmember_user,
                name="My Forbidden Club",
                contact_email="contact@example.com",
            )

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_create_organization_allows_clean_name(
        self, _mock_send_email: MagicMock, nonmember_user: RevelUser
    ) -> None:
        """Test that a name with no reserved tokens passes the guard."""
        org = organization_service.create_organization(
            owner=nonmember_user,
            name="Acoustic Events Collective",
            contact_email="contact@example.com",
        )
        assert org.pk is not None


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
