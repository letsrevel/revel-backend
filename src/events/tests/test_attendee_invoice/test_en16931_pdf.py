"""The BT-114 rounding row on invoice and credit-note PDFs (#1234)."""

from decimal import Decimal

import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, TotalsMethod
from events.tests.test_attendee_invoice._helpers import _create_issued

pytestmark = pytest.mark.django_db

_CONTEXT = {"is_draft": False, "is_export": False, "is_virtual_interim": False}


def _en16931_invoice(
    organization: Organization, event: Event, user: RevelUser, rounding: str = "-0.01"
) -> AttendeeInvoice:
    """An issued 24.90-at-13% invoice (22.04 + 2.87 + rounding) stamped en16931."""
    invoice = _create_issued(organization, event, user)
    invoice.total_gross = Decimal("24.90")
    invoice.total_net = Decimal("22.04")
    invoice.total_vat = Decimal("2.87")
    invoice.rounding_amount = Decimal(rounding)
    invoice.totals_method = TotalsMethod.EN16931
    invoice.vat_rate = Decimal("13.00")
    invoice.line_items = [
        {
            "description": "Event — Tier — Guest",
            "unit_price_gross": "24.90",
            "discount_amount": "0.00",
            "net_amount": "22.04",
            "vat_amount": "2.87",
            "vat_rate": "13.00",
            "vat_category": "S",
        }
    ]
    invoice.save()
    return invoice


def _render_invoice(invoice: AttendeeInvoice) -> str:
    return render_to_string("invoices/attendee_invoice.html", {"invoice": invoice, **_CONTEXT})


class TestInvoicePdf:
    def test_invoice_pdf_shows_rounding_row(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Review focus 1: the unreachable cent is printed, just above the total the buyer paid."""
        html = _render_invoice(_en16931_invoice(organization, event, member_user))
        assert "Rounding" in html
        assert "€-0.01" in html
        assert html.index("Rounding") < html.rindex("Total")
        assert "€24.90" in html

    def test_invoice_pdf_hides_zero_rounding(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _en16931_invoice(organization, event, member_user, rounding="0.00")
        invoice.total_net = Decimal("22.03")  # 22.03 + 2.87 == 24.90: an exactly reachable split
        invoice.save()
        assert "Rounding" not in _render_invoice(invoice)

    def test_legacy_invoice_pdf_unchanged(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Review focus 3: a legacy row never carries a rounding amount, so its PDF has no such row."""
        invoice = _create_issued(organization, event, member_user)
        assert invoice.totals_method == "legacy"
        assert "Rounding" not in _render_invoice(invoice)


class TestCreditNotePdf:
    def test_credit_note_pdf_shows_its_own_rounding(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """The credit note prints its own BT-114, not the invoice's."""
        invoice = _en16931_invoice(organization, event, member_user, rounding="0.00")
        credit_note = AttendeeInvoiceCreditNote.objects.create(
            invoice=invoice,
            credit_note_number="TEST-CN-000001",
            amount_gross=Decimal("24.90"),
            amount_net=Decimal("22.04"),
            amount_vat=Decimal("2.87"),
            rounding_amount=Decimal("-0.01"),
            totals_method=TotalsMethod.EN16931,
            line_items=invoice.line_items,
            issued_at=timezone.now(),
        )
        html = render_to_string(
            "invoices/attendee_credit_note.html", {"credit_note": credit_note, "invoice": invoice, **_CONTEXT}
        )
        assert "Rounding" in html
        assert "€-0.01" in html
        assert html.index("Rounding") < html.index("Total Credited")
