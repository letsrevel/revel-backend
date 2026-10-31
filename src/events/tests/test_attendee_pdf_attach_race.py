"""A slow, unlocked PDF render must never overwrite a PDF attached by a concurrent issuance."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import _generate_and_save_pdf, _generate_credit_note_pdf

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"


def _draft(org: Organization, event: Event, user: RevelUser, n: str) -> AttendeeInvoice:
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_race_{n}",
        invoice_number=f"RACE-{n}",
        status=AttendeeInvoice.InvoiceStatus.DRAFT,
        total_gross=Decimal("10.00"),
        total_net=Decimal("10.00"),
        total_vat=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
        currency="EUR",
        line_items=[],
        seller_name="ACME",
        buyer_name="Buyer",
        buyer_email="buyer@example.de",
    )


def test_draft_render_does_not_replace_issued_pdf(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    """Issuance (with its own PDF) landing mid-render wins; the stale draft render is discarded."""
    invoice = _draft(organization, event, member_user, "1")

    def issue_during_render(*args: t.Any, **kwargs: t.Any) -> bytes:
        row = AttendeeInvoice.objects.get(pk=invoice.pk)
        row.status = AttendeeInvoice.InvoiceStatus.ISSUED
        row.issued_at = timezone.now()
        row.pdf_file.save("issued.pdf", ContentFile(b"issued-pdf"), save=False)
        row.save()
        return b"draft-pdf"

    with patch(MOCK_RENDER_PDF, side_effect=issue_during_render):
        _generate_and_save_pdf(invoice)

    row = AttendeeInvoice.objects.get(pk=invoice.pk)
    assert row.status == AttendeeInvoice.InvoiceStatus.ISSUED
    row.pdf_file.open()
    assert row.pdf_file.read() == b"issued-pdf"


def test_render_discarded_when_status_changed_without_pdf(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    """A draft render is not attached to a row that has meanwhile been issued."""
    invoice = _draft(organization, event, member_user, "2")

    def issue_during_render(*args: t.Any, **kwargs: t.Any) -> bytes:
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(status=AttendeeInvoice.InvoiceStatus.ISSUED)
        return b"draft-pdf"

    with patch(MOCK_RENDER_PDF, side_effect=issue_during_render):
        _generate_and_save_pdf(invoice)

    assert not AttendeeInvoice.objects.get(pk=invoice.pk).pdf_file


def test_render_attaches_when_unchanged(organization: Organization, event: Event, member_user: RevelUser) -> None:
    invoice = _draft(organization, event, member_user, "3")

    with patch(MOCK_RENDER_PDF, return_value=b"draft-pdf"):
        _generate_and_save_pdf(invoice)

    assert AttendeeInvoice.objects.get(pk=invoice.pk).pdf_file
    assert invoice.pdf_file


def test_credit_note_render_does_not_replace_existing_pdf(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _draft(organization, event, member_user, "4")
    cn = AttendeeInvoiceCreditNote.objects.create(
        invoice=invoice,
        credit_note_number="RACE-CN-4",
        amount_gross=Decimal("10.00"),
        amount_net=Decimal("10.00"),
        amount_vat=Decimal("0.00"),
        line_items=[],
        issued_at=timezone.now(),
    )

    def attach_during_render(*args: t.Any, **kwargs: t.Any) -> bytes:
        row = AttendeeInvoiceCreditNote.objects.get(pk=cn.pk)
        row.pdf_file.save("first.pdf", ContentFile(b"first-pdf"), save=False)
        row.save()
        return b"second-pdf"

    with patch(MOCK_RENDER_PDF, side_effect=attach_during_render):
        _generate_credit_note_pdf(cn)

    row = AttendeeInvoiceCreditNote.objects.get(pk=cn.pk)
    row.pdf_file.open()
    assert row.pdf_file.read() == b"first-pdf"
