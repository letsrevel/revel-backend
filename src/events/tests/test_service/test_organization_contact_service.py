"""Tests for organization contact_method validation and self-heal logic."""

from unittest.mock import MagicMock, patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import schema
from events.models import Organization, OrganizationContactMessage
from events.service import organization_service


@pytest.mark.django_db(transaction=True)
class TestUpdateContactEmailSelfHeal:
    """update_contact_email must reset contact_method=NONE on email change."""

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_changing_email_resets_contact_method_to_none(
        self,
        mock_send: MagicMock,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Changing contact_email forces contact_method back to NONE."""
        organization.contact_email = "old@example.com"
        organization.contact_email_verified = True
        organization.contact_method = Organization.ContactMethod.FORM
        organization.save()

        organization_service.update_contact_email(
            organization=organization, new_email="new@example.com", requester=organization_owner_user
        )

        organization.refresh_from_db()
        assert organization.contact_email == "new@example.com"
        assert organization.contact_email_verified is False
        assert organization.contact_method == Organization.ContactMethod.NONE

    @patch("events.tasks.send_organization_contact_email_verification.delay")
    def test_auto_verify_path_keeps_contact_method(
        self,
        mock_send: MagicMock,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """When the new email auto-verifies (matches user.email), contact_method is NOT touched.

        The post-condition (contact_method != NONE → email is verified) is preserved
        without needing the reset.
        """
        organization_owner_user.email = "owner@example.com"
        organization_owner_user.email_verified = True
        organization_owner_user.save()

        organization.contact_email = "old@example.com"
        organization.contact_email_verified = True
        organization.contact_method = Organization.ContactMethod.EMAIL
        organization.save()

        organization_service.update_contact_email(
            organization=organization, new_email="owner@example.com", requester=organization_owner_user
        )

        organization.refresh_from_db()
        assert organization.contact_email == "owner@example.com"
        assert organization.contact_email_verified is True
        assert organization.contact_method == Organization.ContactMethod.EMAIL


@pytest.mark.django_db
class TestValidateContactMethod:
    """organization_service.validate_contact_method enforces the EMAIL/FORM precondition."""

    def test_none_always_allowed(self, organization: Organization) -> None:
        organization.contact_email = None
        organization.contact_email_verified = False
        organization_service.validate_contact_method(organization, Organization.ContactMethod.NONE)

    def test_email_requires_verified_email(self, organization: Organization) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = False
        with pytest.raises(HttpError) as exc_info:
            organization_service.validate_contact_method(organization, Organization.ContactMethod.EMAIL)
        assert exc_info.value.status_code == 400

    def test_form_requires_verified_email(self, organization: Organization) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = False
        with pytest.raises(HttpError) as exc_info:
            organization_service.validate_contact_method(organization, Organization.ContactMethod.FORM)
        assert exc_info.value.status_code == 400

    def test_email_passes_when_verified(self, organization: Organization) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = True
        organization_service.validate_contact_method(organization, Organization.ContactMethod.EMAIL)


@pytest.mark.django_db(transaction=True)
class TestUpdateOrganizationContactMethod:
    """update_organization validates contact_method against the verified-email state."""

    def test_update_rejects_email_without_verified_email(self, organization: Organization) -> None:
        """Setting EMAIL without a verified email is a 400."""
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = False
        organization.save()

        payload = schema.OrganizationEditSchema(
            visibility=Organization.Visibility.PUBLIC,
            contact_method=Organization.ContactMethod.EMAIL,
        )
        with pytest.raises(HttpError) as exc_info:
            organization_service.update_organization(organization, payload, requester=organization.owner)
        assert exc_info.value.status_code == 400

    def test_update_rejects_form_without_verified_email(self, organization: Organization) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = False
        organization.save()

        payload = schema.OrganizationEditSchema(
            visibility=Organization.Visibility.PUBLIC,
            contact_method=Organization.ContactMethod.FORM,
        )
        with pytest.raises(HttpError) as exc_info:
            organization_service.update_organization(organization, payload, requester=organization.owner)
        assert exc_info.value.status_code == 400

    def test_update_allows_form_with_verified_email(self, organization: Organization) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = True
        organization.save()

        payload = schema.OrganizationEditSchema(
            visibility=Organization.Visibility.PUBLIC,
            contact_method=Organization.ContactMethod.FORM,
        )
        organization_service.update_organization(organization, payload, requester=organization.owner)
        organization.refresh_from_db()
        assert organization.contact_method == Organization.ContactMethod.FORM


@pytest.mark.django_db
class TestCreateContactMessage:
    """organization_service.create_contact_message persists messages and rejects bad state."""

    def test_creates_message_when_method_is_form(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        organization.contact_email = "info@example.com"
        organization.contact_email_verified = True
        organization.contact_method = Organization.ContactMethod.FORM
        organization.save()

        with patch("events.tasks.send_organization_contact_message_email.delay"):
            msg = organization_service.create_contact_message(
                organization=organization,
                sender=organization_owner_user,
                subject="Hi",
                message="Hello",
            )

        assert isinstance(msg, OrganizationContactMessage)
        assert msg.sender_id == organization_owner_user.id
        assert msg.sender_email_snapshot == organization_owner_user.email

    def test_rejects_when_method_is_none(self, organization: Organization, organization_owner_user: RevelUser) -> None:
        organization.contact_method = Organization.ContactMethod.NONE
        organization.save()

        with pytest.raises(HttpError) as exc_info:
            organization_service.create_contact_message(
                organization=organization,
                sender=organization_owner_user,
                subject="Hi",
                message="Hello",
            )
        assert exc_info.value.status_code == 400
