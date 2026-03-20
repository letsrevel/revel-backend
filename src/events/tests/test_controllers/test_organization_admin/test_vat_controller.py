"""Tests for organization admin VAT/billing info endpoints (billing-info, vat-id)."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Organization
from events.service.vies_service import VIESUnavailableError, VIESValidationResult

pytestmark = pytest.mark.django_db


# ===========================================================================
# GET /billing-info
# ===========================================================================


class TestGetBillingInfo:
    """Tests for retrieving organization billing info."""

    def test_owner_can_get_billing_info(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the organization owner can retrieve billing info with default values."""
        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == ""
        assert data["vat_id"] == ""
        assert data["vat_country_code"] == ""
        assert data["vat_id_validated"] is False
        assert data["vat_id_validated_at"] is None
        assert data["billing_address"] == ""
        assert data["billing_email"] == ""

    def test_owner_sees_populated_billing_info(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that populated billing info fields are returned correctly."""
        organization.billing_name = "Test Legal Entity S.r.l."
        organization.vat_id = "IT12345678901"
        organization.vat_country_code = "IT"
        organization.vat_id_validated = True
        organization.vat_id_validated_at = timezone.now()
        organization.billing_address = "Via Roma 1, 00100 Roma, Italy"
        organization.billing_email = "billing@org.example.com"
        organization.save()

        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "Test Legal Entity S.r.l."
        assert data["vat_id"] == "IT12345678901"
        assert data["vat_country_code"] == "IT"
        assert data["vat_id_validated"] is True
        assert data["vat_id_validated_at"] is not None
        assert data["billing_address"] == "Via Roma 1, 00100 Roma, Italy"
        assert data["billing_email"] == "billing@org.example.com"

    def test_staff_cannot_get_billing_info(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members are denied access to billing info (owner only)."""
        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_member_cannot_get_billing_info(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        """Test that regular members are denied access to billing info."""
        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = member_client.get(url)

        assert response.status_code == 403

    def test_nonmember_cannot_get_billing_info(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot access billing info (org not in queryset)."""
        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = nonmember_client.get(url)

        # get_object_or_exception returns 404 when org not in queryset
        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_get_billing_info(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users receive 401."""
        url = reverse("api:get_billing_info", kwargs={"slug": organization.slug})

        response = client.get(url)

        assert response.status_code == 401


# ===========================================================================
# PATCH /billing-info
# ===========================================================================


class TestUpdateBillingInfo:
    """Tests for updating organization billing info."""

    def test_update_billing_name(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can update the billing name."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_name": "My Legal Entity GmbH"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "My Legal Entity GmbH"
        organization.refresh_from_db()
        assert organization.billing_name == "My Legal Entity GmbH"

    def test_update_vat_country_code(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can update the VAT country code."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_country_code": "DE"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["vat_country_code"] == "DE"
        organization.refresh_from_db()
        assert organization.vat_country_code == "DE"

    def test_update_billing_address(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can update the billing address."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_address": "Musterstrasse 1, 1010 Wien, Austria"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"] == "Musterstrasse 1, 1010 Wien, Austria"
        organization.refresh_from_db()
        assert organization.billing_address == "Musterstrasse 1, 1010 Wien, Austria"

    def test_update_billing_email(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can update the billing email."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_email": "invoices@example.com"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["billing_email"] == "invoices@example.com"
        organization.refresh_from_db()
        assert organization.billing_email == "invoices@example.com"

    def test_update_vat_rate(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can update the VAT rate."""
        from decimal import Decimal

        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_rate": "22.00"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert Decimal(str(data["vat_rate"])) == Decimal("22.00")
        organization.refresh_from_db()
        assert organization.vat_rate == Decimal("22.00")

    def test_update_multiple_fields_at_once(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that multiple billing fields can be updated in a single request."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {
            "vat_country_code": "FR",
            "billing_address": "123 Rue de Rivoli, Paris",
            "billing_email": "france@example.com",
        }

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["vat_country_code"] == "FR"
        assert data["billing_address"] == "123 Rue de Rivoli, Paris"
        assert data["billing_email"] == "france@example.com"

    def test_null_billing_address_and_email_returns_422_not_500(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Regression: explicit null for billing_address/billing_email must return 422, not 500.

        The DB columns are NOT NULL (default=''). Previously the schema allowed
        None which passed through to the ORM and caused a psycopg NotNullViolation.
        Pydantic now rejects null for these str fields before touching the DB.
        """
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {
            "vat_country_code": "DE",
            "vat_rate": 0.19,
            "billing_name": "Oskar Bechtold",
            "billing_address": None,
            "billing_email": None,
        }

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422

    def test_empty_body_returns_unchanged_org(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that sending an empty body returns the organization unchanged."""
        organization.billing_address = "Original address"
        organization.save()

        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})

        response = organization_owner_client.patch(url, data=orjson.dumps({}), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"] == "Original address"

    def test_country_code_conflicting_with_vat_id_prefix_rejected(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that changing country code to a value conflicting with the VAT ID prefix is rejected.

        When a VAT ID exists (e.g., IT12345678901), the country code must match
        its prefix (IT). Attempting to set a different country code returns 400.
        """
        organization.vat_id = "IT12345678901"
        organization.vat_country_code = "IT"
        organization.save()

        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_country_code": "DE"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_country_code_matching_vat_id_prefix_accepted(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that setting country code matching the existing VAT ID prefix is accepted."""
        organization.vat_id = "DE123456789"
        organization.vat_country_code = "DE"
        organization.save()

        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_country_code": "DE"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["vat_country_code"] == "DE"

    def test_country_code_update_allowed_when_no_vat_id(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that country code can be freely changed when no VAT ID is set."""
        assert organization.vat_id == ""

        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_country_code": "AT"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["vat_country_code"] == "AT"

    def test_invalid_country_code_rejected_by_schema_validation(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a non-EU country code is rejected by schema validation."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"vat_country_code": "US"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422

    def test_staff_cannot_update_billing_info(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot update billing info (owner-only endpoint)."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_address": "Hack attempt"}

        response = organization_staff_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_nonmember_cannot_update_billing_info(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot update billing info."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_address": "Hack attempt"}

        response = nonmember_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_update_billing_info(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot update billing info."""
        url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
        payload = {"billing_address": "Anonymous attempt"}

        response = client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 401


# ===========================================================================
# PUT /vat-id
# ===========================================================================


class TestSetVatId:
    """Tests for setting/updating the organization VAT ID via VIES validation."""

    @patch("events.controllers.organization_admin.vat.validate_and_update_organization")
    def test_valid_vat_id_accepted(
        self,
        mock_validate: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a valid VAT ID is accepted after VIES validation succeeds.

        The controller saves the VAT ID, resets validation status, then calls
        VIES. On success it refreshes from DB and returns updated billing info.
        """
        mock_validate.return_value = VIESValidationResult(
            valid=True,
            name="Test Company",
            address="Via Roma 1, Roma",
            request_identifier="REQ-123",
        )

        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "IT12345678901"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        mock_validate.assert_called_once()
        # Verify the org was passed to the service
        called_org = mock_validate.call_args[0][0]
        assert called_org.id == organization.id
        assert called_org.vat_id == "IT12345678901"
        assert called_org.vat_country_code == "IT"

    @patch("events.controllers.organization_admin.vat.validate_and_update_organization")
    def test_invalid_vat_id_returns_400(
        self,
        mock_validate: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a VAT ID rejected by VIES returns 400."""
        mock_validate.return_value = VIESValidationResult(
            valid=False,
            name="",
            address="",
            request_identifier="REQ-456",
        )

        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "IT00000000000"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    @patch("events.tasks.revalidate_single_vat_id_task.delay")
    @patch("events.controllers.organization_admin.vat.validate_and_update_organization")
    def test_vies_unavailable_returns_503(
        self,
        mock_validate: t.Any,
        mock_revalidate_task: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that VIES unavailability returns 503 and VAT ID is saved but pending.

        The VAT ID should be persisted in the database with vat_id_validated=False
        so it can be retried later. A background revalidation task is queued.
        """
        mock_validate.side_effect = VIESUnavailableError("VIES is down")

        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "DE123456789"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 503
        # Verify the VAT ID was saved despite VIES being unavailable
        organization.refresh_from_db()
        assert organization.vat_id == "DE123456789"
        assert organization.vat_country_code == "DE"
        assert organization.vat_id_validated is False
        assert organization.vat_id_validated_at is None
        # Verify revalidation task was queued
        mock_revalidate_task.assert_called_once_with(str(organization.id))

    @patch("events.controllers.organization_admin.vat.validate_and_update_organization")
    def test_vat_country_code_auto_set_from_prefix(
        self,
        mock_validate: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that vat_country_code is automatically set from the VAT ID prefix.

        Even if the organization had a different country code, setting a VAT ID
        should overwrite it with the prefix of the new ID.
        """
        organization.vat_country_code = "IT"
        organization.save()

        mock_validate.return_value = VIESValidationResult(
            valid=True, name="GmbH", address="Wien", request_identifier="REQ-789"
        )

        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "ATU12345678"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.vat_country_code == "AT"

    @patch("events.controllers.organization_admin.vat.validate_and_update_organization")
    def test_validation_status_reset_before_vies_call(
        self,
        mock_validate: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that vat_id_validated and vat_id_validated_at are reset before VIES call.

        This ensures a previously validated VAT ID does not appear validated during the
        VIES call for a new VAT ID.
        """
        organization.vat_id = "IT12345678901"
        organization.vat_id_validated = True
        organization.vat_id_validated_at = timezone.now()
        organization.save()

        mock_validate.return_value = VIESValidationResult(
            valid=True, name="New Co", address="", request_identifier="REQ-NEW"
        )

        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "DE123456789"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        called_org = mock_validate.call_args[0][0]
        assert called_org.vat_id_validated is False
        assert called_org.vat_id_validated_at is None

    def test_invalid_vat_id_format_rejected_by_schema(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a VAT ID with invalid format is rejected by schema validation.

        The schema enforces a regex: 2-letter country prefix + 2-13 alphanumeric chars.
        """
        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "123"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422

    def test_non_eu_country_prefix_rejected_by_schema(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a VAT ID with a non-EU country prefix is rejected."""
        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "US123456789"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422

    def test_lowercase_vat_id_normalized_to_uppercase(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a lowercase VAT ID is normalized to uppercase by the schema.

        The VATIdUpdateSchema has strip_whitespace=True, to_upper=True.
        """
        with patch("events.controllers.organization_admin.vat.validate_and_update_organization") as mock_validate:
            mock_validate.return_value = VIESValidationResult(
                valid=True, name="Company", address="", request_identifier="REQ-X"
            )

            url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
            payload = {"vat_id": "it12345678901"}

            response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

            assert response.status_code == 200
            organization.refresh_from_db()
            assert organization.vat_id == "IT12345678901"

    def test_staff_cannot_set_vat_id(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot set the VAT ID (owner-only endpoint)."""
        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "IT12345678901"}

        response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_nonmember_cannot_set_vat_id(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot set the VAT ID."""
        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "IT12345678901"}

        response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_set_vat_id(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot set the VAT ID."""
        url = reverse("api:set_vat_id", kwargs={"slug": organization.slug})
        payload = {"vat_id": "IT12345678901"}

        response = client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 401


# ===========================================================================
# DELETE /vat-id
# ===========================================================================


class TestDeleteVatId:
    """Tests for clearing the organization VAT ID."""

    def test_owner_can_delete_vat_id(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can clear the VAT ID and all related fields.

        After deletion, vat_id, vat_country_code, vat_id_validated,
        vat_id_validated_at, and vies_request_identifier should all be cleared.
        """
        organization.vat_id = "IT12345678901"
        organization.vat_country_code = "IT"
        organization.vat_id_validated = True
        organization.vat_id_validated_at = timezone.now()
        organization.vies_request_identifier = "REQ-VALID-123"
        organization.save()

        url = reverse("api:delete_vat_id", kwargs={"slug": organization.slug})

        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        organization.refresh_from_db()
        assert organization.vat_id == ""
        assert organization.vat_country_code == ""
        assert organization.vat_id_validated is False
        assert organization.vat_id_validated_at is None
        assert organization.vies_request_identifier == ""  # type: ignore[unreachable]

    def test_delete_vat_id_when_already_empty(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that deleting a VAT ID when none exists is idempotent (still returns 204)."""
        assert organization.vat_id == ""

        url = reverse("api:delete_vat_id", kwargs={"slug": organization.slug})

        response = organization_owner_client.delete(url)

        assert response.status_code == 204

    def test_staff_cannot_delete_vat_id(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot delete the VAT ID (owner-only endpoint)."""
        url = reverse("api:delete_vat_id", kwargs={"slug": organization.slug})

        response = organization_staff_client.delete(url)

        assert response.status_code == 403

    def test_nonmember_cannot_delete_vat_id(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot delete the VAT ID."""
        url = reverse("api:delete_vat_id", kwargs={"slug": organization.slug})

        response = nonmember_client.delete(url)

        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_delete_vat_id(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot delete the VAT ID."""
        url = reverse("api:delete_vat_id", kwargs={"slug": organization.slug})

        response = client.delete(url)

        assert response.status_code == 401
