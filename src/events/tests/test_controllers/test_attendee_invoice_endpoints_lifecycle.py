"""Integration tests for attendee invoice endpoints: issue, delete, dashboard list/download, VAT payload."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.tests.test_controllers.test_attendee_invoice_endpoints import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft_invoice,
    _create_issued_invoice,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# POST /organization-admin/{slug}/attendee-invoices/{id}/issue
# ---------------------------------------------------------------------------


class TestIssueAttendeeInvoiceEndpoint:
    """Test issuing a draft invoice via API."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_issue_draft_returns_issued_invoice(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Issuing a draft should return status=issued with issued_at set."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="issue1")
        url = reverse(
            "api:issue_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.post(url)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "issued"
        assert data["issued_at"] is not None

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_issuing_a_drifted_draft_is_422(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Regression (#911): issuance is its own gate and used to return 200.

        A drifted DRAFT became a legal document with a PDF delivered to the
        buyer whose header contradicted its own line items.
        """
        invoice = _create_draft_invoice(organization, event, member_user, suffix="issue_drift")
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(total_gross=Decimal("999.00"))
        url = reverse(
            "api:issue_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.post(url, content_type="application/json")

        assert response.status_code == 422
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT
        mock_pdf.assert_not_called()

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_reissue_already_issued_is_idempotent(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Re-issuing an already-issued invoice should succeed (idempotent retry)."""
        invoice = _create_issued_invoice(organization, event, member_user, suffix="issue2")
        url = reverse(
            "api:issue_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.post(url)

        assert response.status_code == 200
        assert response.json()["status"] == "issued"


# ---------------------------------------------------------------------------
# DELETE /organization-admin/{slug}/attendee-invoices/{id}
# ---------------------------------------------------------------------------


class TestDeleteAttendeeInvoiceEndpoint:
    """Test deleting a draft invoice via API."""

    def test_delete_draft_returns_204(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Deleting a draft should return 204 and remove the invoice."""
        invoice = _create_draft_invoice(organization, event, member_user, suffix="del1")
        invoice_id = invoice.id
        url = reverse(
            "api:delete_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not AttendeeInvoice.objects.filter(id=invoice_id).exists()

    def test_cannot_delete_issued_invoice(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Deleting an issued invoice should return 409."""
        invoice = _create_issued_invoice(organization, event, member_user, suffix="del2")
        url = reverse(
            "api:delete_attendee_invoice",
            kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 409


# ---------------------------------------------------------------------------
# GET /dashboard/invoices
# ---------------------------------------------------------------------------


class TestDashboardInvoicesEndpoint:
    """Test listing user's own invoices in the dashboard."""

    def test_user_sees_only_issued_invoices(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """User should only see their own ISSUED invoices, not drafts."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        _create_draft_invoice(organization, event, member_user, suffix="dash_draft")
        _create_issued_invoice(organization, event, member_user, suffix="dash_issued")

        url = reverse("api:dashboard_invoices")
        response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        # PaginatedResponseSchema uses "results" key
        assert data["results"][0]["status"] == "issued"

    def test_user_does_not_see_other_users_invoices(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """User should not see invoices belonging to other users."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        # Create an issued invoice for a different user
        _create_issued_invoice(organization, event, organization_owner_user, suffix="other_user")

        url = reverse("api:dashboard_invoices")
        response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# GET /dashboard/invoices/{id}/download
# ---------------------------------------------------------------------------


class TestDashboardInvoiceDownloadEndpoint:
    """Test downloading an invoice PDF from the dashboard."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_download_issued_invoice(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """User can download their own issued invoice."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        invoice = _create_issued_invoice(organization, event, member_user, suffix="dl1")

        url = reverse("api:dashboard_invoice_download", kwargs={"invoice_id": str(invoice.id)})
        response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert "download_url" in data

    def test_cannot_download_draft_invoice(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Draft invoices should not be downloadable from the dashboard."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        invoice = _create_draft_invoice(organization, event, member_user, suffix="dl2")

        url = reverse("api:dashboard_invoice_download", kwargs={"invoice_id": str(invoice.id)})
        response = client.get(url)

        assert response.status_code == 404

    def test_cannot_download_other_users_invoice(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """User cannot download an invoice belonging to another user."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        invoice = _create_issued_invoice(organization, event, organization_owner_user, suffix="dl3")

        url = reverse("api:dashboard_invoice_download", kwargs={"invoice_id": str(invoice.id)})
        response = client.get(url)

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Mixed-rate VAT breakdown on the invoice payloads (#897)
# ---------------------------------------------------------------------------


class TestInvoiceVatBreakdownPayload:
    """The buyer-facing invoice payload must never imply a single rate it cannot support."""

    def test_mixed_rate_invoice_exposes_flag_and_per_rate_buckets(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """A 22%/10% invoice reports has_mixed_vat_rates and both buckets on /dashboard/invoices.

        This is the payload `/account/invoices` renders; before #897 it carried only
        the scalar `vat_rate` (the first payment's), so the page printed one rate
        for a cart that had two.
        """
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        invoice = _create_issued_invoice(organization, event, member_user, suffix="mixed_vat")
        invoice.line_items = [
            {
                "description": "Mixed Event — Standard — Ann",
                "unit_price_gross": "50.00",
                "discount_amount": "0.00",
                "net_amount": "40.98",
                "vat_amount": "9.02",
                "vat_rate": "22.00",
            },
            {
                "description": "Mixed Event — VIP — Bob",
                "unit_price_gross": "120.00",
                "discount_amount": "0.00",
                "net_amount": "109.09",
                "vat_amount": "10.91",
                "vat_rate": "10.00",
            },
        ]
        invoice.total_gross = Decimal("170.00")
        invoice.total_net = Decimal("150.07")
        invoice.total_vat = Decimal("19.93")
        invoice.save()

        response = client.get(reverse("api:dashboard_invoices"))

        assert response.status_code == 200
        payload = response.json()["results"][0]
        assert payload["has_mixed_vat_rates"] is True
        assert payload["vat_breakdown"] == [
            {
                "vat_rate": "10.00",
                "net_amount": "109.09",
                "vat_amount": "10.91",
                "gross_amount": "120.00",
            },
            {
                "vat_rate": "22.00",
                "net_amount": "40.98",
                "vat_amount": "9.02",
                "gross_amount": "50.00",
            },
        ]

    def test_single_rate_invoice_reports_not_mixed_with_one_bucket(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """The ordinary single-rate invoice keeps a renderable scalar rate."""
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        _create_issued_invoice(organization, event, member_user, suffix="single_vat")

        response = client.get(reverse("api:dashboard_invoices"))

        assert response.status_code == 200
        payload = response.json()["results"][0]
        assert payload["has_mixed_vat_rates"] is False
        assert len(payload["vat_breakdown"]) == 1
        assert payload["vat_breakdown"][0]["vat_rate"] == "22.00"
        assert payload["vat_rate"] == "22.00"
