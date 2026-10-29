"""Structured buyer address editing on attendee invoices (PATCH .../attendee-invoices/{id})."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, Organization
from events.tests.test_controllers.test_attendee_invoice_endpoints import (
    _create_draft_invoice,
    _create_issued_invoice,
)

pytestmark = pytest.mark.django_db


class TestUpdateAttendeeInvoiceAddressEndpoint:
    """PATCH of the structured buyer address."""

    def test_draft_patch_updates_buyer_address(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Owner can edit the structured buyer address on a draft; country is uppercased."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="addr1")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(
                {
                    "buyer_address_line1": "Hauptstr. 1",
                    "buyer_postal_code": "10115",
                    "buyer_city": "Berlin",
                    "buyer_country_code": "de",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["buyer_address_line1"] == "Hauptstr. 1"
        assert data["buyer_country_code"] == "DE"
        assert data["seller_country_code"] == ""
        invoice.refresh_from_db()
        assert invoice.buyer_postal_address.is_complete

    def test_draft_patch_rejects_invalid_buyer_country(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """An invalid buyer country code is a validation error."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="addr3")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"buyer_country_code": "ZZ"}), content_type="application/json"
        )
        assert response.status_code == 422

    def test_issued_invoice_rejects_buyer_address_patch(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """An issued invoice's buyer address cannot be edited."""
        invoice = _create_issued_invoice(organization, event, member_user, suffix="addr2")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"buyer_city": "Berlin"}), content_type="application/json"
        )
        assert response.status_code == 409


class TestUpdateAttendeeInvoiceAddressLimits:
    """Length limits on the draft PATCH buyer address mirror the model columns."""

    @pytest.mark.parametrize(
        "field,limit",
        [
            ("buyer_address_line1", 255),
            ("buyer_address_line2", 255),
            ("buyer_postal_code", 20),
            ("buyer_city", 100),
            ("buyer_subdivision", 100),
        ],
    )
    def test_overlong_value_is_422_naming_the_field(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        field: str,
        limit: int,
    ) -> None:
        invoice = _create_draft_invoice(organization, event, member_user, suffix=f"lim-{field}")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({field: "x" * (limit + 1)}), content_type="application/json"
        )
        assert response.status_code == 422
        assert field in response.content.decode()

    def test_values_are_stripped(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        invoice = _create_draft_invoice(organization, event, member_user, suffix="lim-strip")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"buyer_city": "  Berlin  "}), content_type="application/json"
        )
        assert response.status_code == 200
        assert response.json()["buyer_city"] == "Berlin"
