"""Tests for attendee invoice delivery, PDF generation, and slug sanitization.

Tests cover:
- deliver_attendee_invoice() -- no-PDF and no-recipient edge cases
- deliver_credit_note() -- no-PDF edge case
- ensure_pdf_exists() -- on-demand PDF generation
- _sanitize_org_slug() -- truncation and invoice number length guarantees
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import (
    _CREDIT_NOTE_SLUG_MAX,
    _INVOICE_SLUG_MAX,
    _get_org_invoice_prefix,
    _sanitize_org_slug,
    deliver_attendee_invoice,
    deliver_credit_note,
    ensure_pdf_exists,
)

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"
MOCK_SEND_EMAIL = "common.tasks.send_email"

_COUNTER = 0


def _create_invoice(org: Organization, event: Event, user: RevelUser, *, issued: bool = True) -> AttendeeInvoice:
    """Create a minimal invoice for testing."""
    global _COUNTER  # noqa: PLW0603
    _COUNTER += 1
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_dlv_{_COUNTER}",
        invoice_number=f"DLV-{_COUNTER:06d}",
        status=AttendeeInvoice.InvoiceStatus.ISSUED if issued else AttendeeInvoice.InvoiceStatus.DRAFT,
        issued_at=timezone.now() if issued else None,
        total_gross=Decimal("100.00"),
        total_net=Decimal("81.97"),
        total_vat=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        currency="EUR",
        line_items=[],
        seller_name="ACME SRL",
        seller_email="billing@acme.it",
        buyer_name="Buyer GmbH",
        buyer_email="buyer@example.de",
    )


# ---------------------------------------------------------------------------
# deliver_attendee_invoice / deliver_credit_note
# ---------------------------------------------------------------------------


class TestDeliverAttendeeInvoice:
    """Test invoice/credit note delivery edge cases."""

    @patch(MOCK_SEND_EMAIL)
    def test_no_pdf_skips_delivery(
        self,
        mock_email: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Invoice without PDF should log warning and skip email."""
        inv = _create_invoice(organization, event, member_user)
        assert not inv.pdf_file
        deliver_attendee_invoice(inv)
        mock_email.delay.assert_not_called()

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_no_recipient_skips_delivery(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Invoice with no buyer_email and no user email should skip email."""
        inv = _create_invoice(organization, event, member_user)
        inv.buyer_email = ""
        inv.save(update_fields=["buyer_email"])
        member_user.email = ""
        member_user.save(update_fields=["email"])
        ensure_pdf_exists(inv)
        inv.refresh_from_db()
        deliver_attendee_invoice(inv)
        mock_email.delay.assert_not_called()

    @patch(MOCK_SEND_EMAIL)
    def test_credit_note_no_pdf_skips_delivery(
        self,
        mock_email: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Credit note without PDF should log warning and skip email."""
        inv = _create_invoice(organization, event, member_user)
        cn = AttendeeInvoiceCreditNote.objects.create(
            invoice=inv,
            credit_note_number="DLV-CN-000001",
            amount_gross=Decimal("100.00"),
            amount_net=Decimal("81.97"),
            amount_vat=Decimal("18.03"),
            line_items=[],
            issued_at=timezone.now(),
        )
        assert not cn.pdf_file
        deliver_credit_note(cn)
        mock_email.delay.assert_not_called()


# ---------------------------------------------------------------------------
# _sanitize_org_slug
# ---------------------------------------------------------------------------


class TestSanitizeOrgSlug:
    """Test org slug sanitization for invoice number prefixes."""

    def test_basic_slug(self, organization: Organization) -> None:
        """Normal slug should be uppercased with hyphens removed."""
        organization.slug = "my-org"
        assert _sanitize_org_slug(organization, 38) == "MYORG"

    def test_truncation(self, organization: Organization) -> None:
        """Long slugs should be truncated to max_len."""
        organization.slug = "a" * 60
        result = _sanitize_org_slug(organization, 10)
        assert result == "A" * 10

    def test_invoice_number_fits_max_length(self, organization: Organization) -> None:
        """Invoice number with max-length slug should fit in 50 chars."""
        organization.slug = "a" * 50  # longer than limit, will be truncated
        prefix = _get_org_invoice_prefix(organization)
        # Worst case: {prefix}{YEAR}-{SEQ:06d} = prefix + 11 chars
        invoice_number = f"{prefix}2026-000001"
        assert len(invoice_number) <= 50
        assert len(_sanitize_org_slug(organization, _INVOICE_SLUG_MAX)) == _INVOICE_SLUG_MAX

    def test_credit_note_number_fits_max_length(self, organization: Organization) -> None:
        """Credit note number with max-length slug should fit in 50 chars."""
        organization.slug = "a" * 50
        sanitized = _sanitize_org_slug(organization, _CREDIT_NOTE_SLUG_MAX)
        # Credit note: {sanitized}-CN-{YEAR}-{SEQ:06d}
        cn_number = f"{sanitized}-CN-2026-000001"
        assert len(cn_number) <= 50
        assert len(sanitized) == _CREDIT_NOTE_SLUG_MAX


# ---------------------------------------------------------------------------
# ensure_pdf_exists
# ---------------------------------------------------------------------------


class TestEnsurePdfExists:
    """Test on-demand PDF generation."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_generates_pdf_when_missing(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """PDF should be generated when invoice has no pdf_file."""
        inv = _create_invoice(organization, event, member_user)
        assert not inv.pdf_file
        ensure_pdf_exists(inv)
        inv.refresh_from_db()
        assert inv.pdf_file
        mock_pdf.assert_called_once()

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_skips_generation_when_pdf_exists(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """No PDF generation if pdf_file already exists."""
        inv = _create_invoice(organization, event, member_user)
        # Generate PDF first
        ensure_pdf_exists(inv)
        inv.refresh_from_db()
        assert inv.pdf_file
        mock_pdf.reset_mock()
        # Second call should skip
        ensure_pdf_exists(inv)
        mock_pdf.assert_not_called()
