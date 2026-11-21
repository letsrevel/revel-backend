"""Integration tests for attendee invoice controller endpoints.

Tests cover:
- PATCH /organization-admin/{slug}/invoicing -- set invoicing mode with validation
- POST /events/{event_id}/tickets/vat-preview -- VAT preview with VIES validation
- GET /organization-admin/{slug}/attendee-invoices -- list org invoices
- PATCH /organization-admin/{slug}/attendee-invoices/{id} -- edit draft
- POST /organization-admin/{slug}/attendee-invoices/{id}/issue -- issue draft
- DELETE /organization-admin/{slug}/attendee-invoices/{id} -- delete draft
- GET /dashboard/invoices -- list user's issued invoices
- GET /dashboard/invoices/{id}/download -- download invoice PDF
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"
MOCK_SEND_EMAIL = "common.tasks.send_email"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_invoicing_ready(org: Organization) -> Organization:
    """Configure an org with all prerequisites for invoicing."""
    org.vat_country_code = "IT"
    org.vat_id = "IT12345678901"
    org.vat_id_validated = True
    org.vat_rate = Decimal("22.00")
    org.billing_name = "ACME SRL"
    org.billing_address = "Via Roma 1, 00100 Roma"
    org.billing_address_line1 = "Via Roma 1"
    org.billing_postal_code = "00100"
    org.billing_city = "Roma"
    org.billing_country_code = "IT"
    org.billing_email = "billing@acme.it"
    org.contact_email = "info@acme.it"
    org.save()
    return org


def _create_draft_invoice(org: Organization, event: Event, user: RevelUser, suffix: str = "1") -> AttendeeInvoice:
    """Create a draft invoice for controller tests."""
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_ctrl_{suffix}",
        invoice_number=f"ORG-2026-{suffix.zfill(6)}",
        status=AttendeeInvoice.InvoiceStatus.DRAFT,
        total_gross=Decimal("100.00"),
        total_net=Decimal("81.97"),
        total_vat=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        currency="EUR",
        line_items=[
            {
                "description": "Event - Tier - Guest",
                "unit_price_gross": "100.00",
                "discount_amount": "0.00",
                "net_amount": "81.97",
                "vat_amount": "18.03",
                "vat_rate": "22.00",
            }
        ],
        seller_name="ACME SRL",
        seller_vat_id="IT12345678901",
        seller_vat_country="IT",
        seller_address="Via Roma 1",
        seller_email="billing@acme.it",
        buyer_name="Buyer GmbH",
        buyer_vat_id="DE123456789",
        buyer_vat_country="DE",
        buyer_address="Berlin",
        buyer_email="buyer@example.de",
    )


def _create_issued_invoice(org: Organization, event: Event, user: RevelUser, suffix: str = "1") -> AttendeeInvoice:
    """Create an issued invoice for controller tests."""
    invoice = _create_draft_invoice(org, event, user, suffix=f"issued_{suffix}")
    invoice.status = AttendeeInvoice.InvoiceStatus.ISSUED
    invoice.issued_at = timezone.now()
    invoice.save()
    return invoice


# ---------------------------------------------------------------------------
# PATCH /organization-admin/{slug}/invoicing
# ---------------------------------------------------------------------------


class TestSetInvoicingModeEndpoint:
    """Test the set_invoicing_mode controller endpoint."""

    def test_set_none_succeeds(self, organization_owner_client: Client, organization: Organization) -> None:
        """Setting NONE always succeeds, no prerequisites needed."""
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"mode": "none"}),
            content_type="application/json",
        )

        assert response.status_code == 200

    def test_set_hybrid_without_prerequisites_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Setting HYBRID without prerequisites returns 422."""
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"mode": "hybrid"}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_set_hybrid_with_prerequisites_succeeds(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Setting HYBRID with all prerequisites returns 200."""
        _make_org_invoicing_ready(organization)
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"mode": "hybrid"}),
            content_type="application/json",
        )

        assert response.status_code == 200

    def test_set_auto_with_prerequisites_succeeds(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Setting AUTO with all prerequisites returns 200."""
        _make_org_invoicing_ready(organization)
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"mode": "auto"}),
            content_type="application/json",
        )

        assert response.status_code == 200

    def test_nonowner_cannot_set_invoicing_mode(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: t.Any,
    ) -> None:
        """Non-owner (staff) should be denied access."""
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = organization_staff_client.patch(
            url,
            data=orjson.dumps({"mode": "none"}),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_unauthenticated_is_denied(self, organization: Organization) -> None:
        """Unauthenticated request should be denied."""
        client = Client()
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})
        response = client.patch(
            url,
            data=orjson.dumps({"mode": "none"}),
            content_type="application/json",
        )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /events/{event_id}/tickets/vat-preview
# ---------------------------------------------------------------------------


class TestVATPreviewEndpoint:
    """Test the VAT preview controller endpoint."""

    @patch("common.service.vies_service.validate_vat_id_cached")
    def test_vat_preview_domestic_b2c(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Domestic B2C preview should show full VAT."""
        _make_org_invoicing_ready(organization)
        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})

        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Mario Rossi",
                        "vat_country_code": "IT",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["reverse_charge"] is False
        assert len(data["line_items"]) == 1
        # No VAT ID provided, so VIES should not be called
        mock_vies.assert_not_called()

    @patch("common.service.vies_service.validate_vat_id_cached")
    def test_vat_preview_validated_vat_id_no_longer_discounts_the_price(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """EU B2B cross-border: VIES still validates (for invoice display) but the price stays gross (#868)."""
        from common.service.vies_service import VIESValidationResult

        _make_org_invoicing_ready(organization)
        mock_vies.return_value = VIESValidationResult(
            valid=True, name="Buyer GmbH", address="Berlin", request_identifier="R1"
        )

        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Buyer GmbH",
                        "vat_id": "DE123456789",
                        "vat_country_code": "DE",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id_valid"] is True
        assert data["reverse_charge"] is False
        # Full gross at the org's 22% rate: 10.00 = 8.20 net + 1.80 VAT
        assert Decimal(data["total_gross"]) == Decimal("10.00")
        assert Decimal(data["total_vat"]) == Decimal("1.80")

    @patch("common.service.vies_service.validate_vat_id_cached")
    def test_vat_preview_vies_unavailable(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """When VIES is unavailable, vat_id_valid should be None."""
        from common.service.vies_service import VIESUnavailableError

        _make_org_invoicing_ready(organization)
        mock_vies.side_effect = VIESUnavailableError("VIES is down")

        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Buyer GmbH",
                        "vat_id": "DE123456789",
                        "vat_country_code": "DE",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id_valid"] is None
        assert data["vat_id_validation_error"] is not None

    @patch("common.service.vies_service.validate_vat_id_cached")
    def test_vat_preview_invalid_vat_format(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Invalid VAT ID format should return vat_id_valid=False."""
        _make_org_invoicing_ready(organization)
        mock_vies.side_effect = ValueError("Invalid VAT ID format")

        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Bad VAT",
                        "vat_id": "X",
                        "vat_country_code": "IT",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id_valid"] is False
        assert "Invalid VAT ID format" in data["vat_id_validation_error"]

    def test_vat_preview_invalid_tier_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
    ) -> None:
        """Non-existent tier should return 404."""
        _make_org_invoicing_ready(organization)
        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {"billing_name": "Buyer"},
                    "items": [{"tier_id": "00000000-0000-0000-0000-000000000000", "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /organization-admin/{slug}/attendee-invoices
# ---------------------------------------------------------------------------


class TestListAttendeeInvoicesEndpoint:
    """Test listing attendee invoices for an organization."""

    def test_owner_can_list_invoices(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Org owner should see all invoices for the organization."""
        _create_draft_invoice(organization, event, member_user, suffix="list1")
        _create_issued_invoice(organization, event, member_user, suffix="list2")

        url = reverse("api:list_attendee_invoices", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_empty_list_when_no_invoices(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """No invoices should return an empty list."""
        url = reverse("api:list_attendee_invoices", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0

    def test_nonowner_cannot_list_invoices(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: t.Any,
    ) -> None:
        """Non-owner should be denied access."""
        url = reverse("api:list_attendee_invoices", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /organization-admin/{slug}/attendee-invoices/{id}
# ---------------------------------------------------------------------------


class TestUpdateAttendeeInvoiceEndpoint:
    """Test editing draft invoices via API."""

    def test_owner_can_edit_draft_buyer_name(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Owner can edit buyer_name on a draft invoice."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit1")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"buyer_name": "Updated Buyer"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["buyer_name"] == "Updated Buyer"

    def test_cannot_edit_issued_invoice(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Editing an issued invoice should return 409."""
        invoice = _create_issued_invoice(organization, event, member_user, suffix="edit2")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"buyer_name": "Hacked"}),
            content_type="application/json",
        )

        assert response.status_code == 409

    def test_unknown_fields_are_rejected_by_schema(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Unknown fields (e.g. seller_name) are refused, not silently dropped (#911).

        The schema was permissive, so a caller editing a non-editable field got a
        200 and no change. ``extra="forbid"`` tells them instead.
        """
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit3")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"seller_name": "Hacked Corp"}),
            content_type="application/json",
        )

        assert response.status_code == 422
        invoice.refresh_from_db()
        assert invoice.seller_name == "ACME SRL"  # unchanged

    @pytest.mark.parametrize("field", ["total_gross", "total_net", "total_vat", "vat_rate", "discount_amount_total"])
    def test_derived_totals_are_not_writable(
        self,
        field: str,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Regression (#911): PATCHing a header total alone used to return 200 and drift the invoice."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix=f"edit_derived_{field}")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({field: "999.00"}),
            content_type="application/json",
        )

        assert response.status_code == 422
        invoice.refresh_from_db()
        assert invoice.total_gross == Decimal("100.00")

    def test_editing_line_items_recomputes_totals_in_the_response(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """The header and the vat_breakdown in one response agree, by construction (#911)."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit_items")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(
                {
                    "line_items": [
                        {
                            "description": "Reduced rate ticket",
                            "unit_price_gross": "22.00",
                            "discount_amount": "0.00",
                            "net_amount": "20.00",
                            "vat_amount": "2.00",
                            "vat_rate": "10.00",
                        }
                    ]
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert Decimal(data["total_gross"]) == Decimal("22.00")
        assert Decimal(data["total_net"]) == Decimal("20.00")
        assert Decimal(data["total_vat"]) == Decimal("2.00")
        assert Decimal(data["vat_rate"]) == Decimal("10.00")
        assert [Decimal(b["gross_amount"]) for b in data["vat_breakdown"]] == [Decimal("22.00")]

    @pytest.mark.parametrize("submitted", ["Z", ""])
    def test_submitted_vat_category_is_ignored_and_server_derived(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        submitted: str,
    ) -> None:
        invoice = _create_draft_invoice(organization, event, member_user, suffix=f"cat_{submitted or 'empty'}")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        line = {**invoice.line_items[0], "vat_category": submitted}
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"line_items": [line]}), content_type="application/json"
        )

        assert response.status_code == 200
        assert response.json()["line_items"][0]["vat_category"] == "S"

    def test_explicit_null_line_items_is_422(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """An explicit `{"line_items": null}` must be rejected, not crash the normalizer (#908)."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit_null_items")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"line_items": None}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_explicit_null_total_gross_is_422(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """An explicit `{"total_gross": null}` must be rejected, not violate the NOT NULL column (#908).

        Since #911 the rejection comes one layer earlier -- ``total_gross`` is no
        longer part of the write schema at all -- but the contract is the same 422.
        """
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit_null_gross")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"total_gross": None}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_explicit_null_buyer_vat_id_still_blanks_to_empty_string(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Blankable string fields keep coercing null -> "" -- the #908 guard must not regress this."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="edit_null_vat_id")
        url = reverse(
            "api:update_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps({"buyer_vat_id": None}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["buyer_vat_id"] == ""
