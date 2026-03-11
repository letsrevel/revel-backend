"""Tests for organization admin platform fee invoice and credit note endpoints."""

import typing as t
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Organization, PlatformFeeCreditNote, PlatformFeeInvoice

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_invoice(
    organization: Organization,
    *,
    number_suffix: str = "001",
    period_start: date | None = None,
    status: str = PlatformFeeInvoice.InvoiceStatus.ISSUED,
    with_pdf: bool = False,
) -> PlatformFeeInvoice:
    """Create a PlatformFeeInvoice for the given organization."""
    start = period_start or date(2025, 1, 1)
    invoice = PlatformFeeInvoice.objects.create(
        organization=organization,
        invoice_number=f"RVL-2025-{number_suffix}",
        period_start=start,
        period_end=start + timedelta(days=30),
        fee_gross=Decimal("12.20"),
        fee_net=Decimal("10.00"),
        fee_vat=Decimal("2.20"),
        fee_vat_rate=Decimal("22.00"),
        currency="EUR",
        reverse_charge=False,
        org_name=organization.name,
        org_vat_id=organization.vat_id,
        org_vat_country=organization.vat_country_code,
        platform_business_name="Revel GmbH",
        platform_business_address="Vienna, Austria",
        platform_vat_id="ATU12345678",
        total_tickets=5,
        total_ticket_revenue=Decimal("100.00"),
        status=status,
        issued_at=timezone.now() if status != PlatformFeeInvoice.InvoiceStatus.DRAFT else None,
    )
    if with_pdf:
        invoice.pdf_file.name = "invoices/platform_fee/test.pdf"
        invoice.save(update_fields=["pdf_file"])
    return invoice


def _create_credit_note(
    invoice: PlatformFeeInvoice,
    *,
    number_suffix: str = "001",
) -> PlatformFeeCreditNote:
    """Create a PlatformFeeCreditNote for the given invoice."""
    return PlatformFeeCreditNote.objects.create(
        invoice=invoice,
        credit_note_number=f"RVL-CN-2025-{number_suffix}",
        fee_gross=Decimal("12.20"),
        fee_net=Decimal("10.00"),
        fee_vat=Decimal("2.20"),
        issued_at=timezone.now(),
    )


# ===========================================================================
# GET /invoices
# ===========================================================================


class TestListInvoices:
    """Tests for listing platform fee invoices."""

    def test_owner_can_list_invoices(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can list invoices with correct pagination structure."""
        _create_invoice(organization, number_suffix="001")
        _create_invoice(organization, number_suffix="002", period_start=date(2025, 2, 1))

        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "results" in data
        assert data["count"] == 2
        assert len(data["results"]) == 2

    def test_invoice_fields_in_response(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that individual invoice entries contain all expected fields."""
        invoice = _create_invoice(organization)

        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        result = data["results"][0]
        assert result["id"] == str(invoice.id)
        assert result["invoice_number"] == "RVL-2025-001"
        assert result["period_start"] == "2025-01-01"
        assert result["status"] == "issued"
        assert result["fee_gross"] == "12.20"
        assert result["fee_net"] == "10.00"
        assert result["fee_vat"] == "2.20"
        assert result["fee_vat_rate"] == "22.00"
        assert result["currency"] == "EUR"
        assert result["reverse_charge"] is False
        assert result["total_tickets"] == 5
        assert result["total_ticket_revenue"] == "100.00"

    def test_empty_invoice_list(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that an empty invoice list returns 200 with empty results."""
        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_invoices_ordered_by_period_start_descending(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that invoices are returned ordered by period_start descending (newest first)."""
        _create_invoice(organization, number_suffix="001", period_start=date(2025, 1, 1))
        _create_invoice(organization, number_suffix="002", period_start=date(2025, 3, 1))
        _create_invoice(organization, number_suffix="003", period_start=date(2025, 2, 1))

        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        results = response.json()["results"]
        dates = [r["period_start"] for r in results]
        assert dates == ["2025-03-01", "2025-02-01", "2025-01-01"]

    def test_invoices_paginated(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that invoice listing is paginated with page_size=20."""
        for i in range(25):
            _create_invoice(
                organization,
                number_suffix=f"{i:03d}",
                period_start=date(2020, 1, 1) + timedelta(days=31 * i),
            )

        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        # Page 1
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 25
        assert len(data["results"]) == 20

        # Page 2
        response = organization_owner_client.get(url, {"page": 2})
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 5

    def test_invoices_scoped_to_organization(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that invoices from other organizations are not visible."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=nonmember_user)
        _create_invoice(organization, number_suffix="001")
        _create_invoice(other_org, number_suffix="999")

        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["invoice_number"] == "RVL-2025-001"

    def test_staff_cannot_list_invoices(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot list invoices (owner-only endpoint)."""
        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_nonmember_cannot_list_invoices(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot list invoices."""
        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = nonmember_client.get(url)

        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_list_invoices(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot list invoices."""
        url = reverse("api:list_invoices", kwargs={"slug": organization.slug})

        response = client.get(url)

        assert response.status_code == 401


# ===========================================================================
# GET /invoices/{invoice_id}
# ===========================================================================


class TestGetInvoice:
    """Tests for retrieving a specific platform fee invoice."""

    def test_owner_can_get_invoice(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can retrieve a specific invoice by ID."""
        invoice = _create_invoice(organization)

        url = reverse(
            "api:get_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(invoice.id)
        assert data["invoice_number"] == invoice.invoice_number

    def test_invoice_from_different_org_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that requesting an invoice from another organization returns 404.

        Even if the invoice exists, scoping to the current organization
        should prevent access.
        """
        other_org = Organization.objects.create(name="Other Org", slug="other-org-detail", owner=nonmember_user)
        other_invoice = _create_invoice(other_org, number_suffix="OTHER")

        url = reverse(
            "api:get_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(other_invoice.id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_nonexistent_invoice_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that requesting a non-existent invoice ID returns 404."""
        fake_id = uuid4()
        url = reverse(
            "api:get_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(fake_id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_staff_cannot_get_invoice(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot get invoice details."""
        invoice = _create_invoice(organization)

        url = reverse(
            "api:get_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_unauthenticated_cannot_get_invoice(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot get invoice details."""
        invoice = _create_invoice(organization)

        url = reverse(
            "api:get_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = client.get(url)

        assert response.status_code == 401


# ===========================================================================
# GET /invoices/{invoice_id}/download
# ===========================================================================


class TestDownloadInvoice:
    """Tests for downloading invoice PDFs."""

    @patch("events.controllers.organization_admin.vat.get_file_url")
    def test_owner_can_download_invoice_with_pdf(
        self,
        mock_get_file_url: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner gets a signed download URL for an invoice PDF."""
        mock_get_file_url.return_value = "https://cdn.example.com/signed/invoice.pdf?sig=abc"
        invoice = _create_invoice(organization, with_pdf=True)

        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert "download_url" in data
        assert data["download_url"] == "https://cdn.example.com/signed/invoice.pdf?sig=abc"
        mock_get_file_url.assert_called_once()

    @patch("events.controllers.organization_admin.vat.get_file_url")
    def test_invoice_without_pdf_returns_404(
        self,
        mock_get_file_url: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that downloading an invoice without a generated PDF returns 404."""
        mock_get_file_url.return_value = None
        invoice = _create_invoice(organization)

        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_download_invoice_from_different_org_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that downloading an invoice from another org returns 404."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org-download", owner=nonmember_user)
        other_invoice = _create_invoice(other_org, number_suffix="DL-OTHER", with_pdf=True)

        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(other_invoice.id)},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_download_nonexistent_invoice_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that downloading a non-existent invoice ID returns 404."""
        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(uuid4())},
        )

        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_staff_cannot_download_invoice(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot download invoices."""
        invoice = _create_invoice(organization, with_pdf=True)

        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_unauthenticated_cannot_download_invoice(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot download invoices."""
        invoice = _create_invoice(organization, with_pdf=True)

        url = reverse(
            "api:download_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )

        response = client.get(url)

        assert response.status_code == 401


# ===========================================================================
# GET /credit-notes
# ===========================================================================


class TestListCreditNotes:
    """Tests for listing platform fee credit notes."""

    def test_owner_can_list_credit_notes(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the owner can list credit notes for the organization."""
        invoice = _create_invoice(organization)
        _create_credit_note(invoice, number_suffix="001")
        _create_credit_note(invoice, number_suffix="002")

        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["results"]) == 2

    def test_credit_note_fields_in_response(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that credit note entries contain all expected fields."""
        invoice = _create_invoice(organization)
        credit_note = _create_credit_note(invoice)

        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["id"] == str(credit_note.id)
        assert result["credit_note_number"] == "RVL-CN-2025-001"
        assert result["invoice_id"] == str(invoice.id)
        assert result["fee_gross"] == "12.20"
        assert result["fee_net"] == "10.00"
        assert result["fee_vat"] == "2.20"
        assert result["issued_at"] is not None

    def test_empty_credit_notes_list(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that an empty credit notes list returns 200 with empty results."""
        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_credit_notes_scoped_to_organization(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that credit notes from other organizations are not visible."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org-cn", owner=nonmember_user)
        our_invoice = _create_invoice(organization, number_suffix="OUR")
        their_invoice = _create_invoice(other_org, number_suffix="THEIR")
        _create_credit_note(our_invoice, number_suffix="OUR-CN")
        _create_credit_note(their_invoice, number_suffix="THEIR-CN")

        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["credit_note_number"] == "RVL-CN-2025-OUR-CN"

    def test_staff_cannot_list_credit_notes(
        self,
        organization_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Test that staff members cannot list credit notes (owner-only endpoint)."""
        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_nonmember_cannot_list_credit_notes(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that non-members cannot list credit notes."""
        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = nonmember_client.get(url)

        assert response.status_code in (403, 404)

    def test_unauthenticated_cannot_list_credit_notes(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that unauthenticated users cannot list credit notes."""
        url = reverse("api:list_credit_notes", kwargs={"slug": organization.slug})

        response = client.get(url)

        assert response.status_code == 401
