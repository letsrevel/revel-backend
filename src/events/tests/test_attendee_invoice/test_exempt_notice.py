"""The VAT-exempt notice on invoice PDFs is independent of the export / interim notices (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import _is_export, generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db

REASON = "§4 Nr. 20 UStG"
INVOICE = "invoices/attendee_invoice.html"
CREDIT_NOTE = "invoices/attendee_credit_note.html"


def _line(category: str | None, rate: str) -> dict[str, t.Any]:
    line: dict[str, t.Any] = {
        "description": "Ticket",
        "unit_price_gross": "10.00",
        "discount_amount": "0.00",
        "net_amount": "10.00",
        "vat_amount": "0.00",
        "vat_rate": rate,
    }
    if category:
        line["vat_category"] = category
    return line


def _doc(**fields: t.Any) -> AttendeeInvoice:
    totals = {"total_gross": Decimal("10"), "total_net": Decimal("10"), "total_vat": Decimal("0"), "currency": "EUR"}
    return AttendeeInvoice(**{**totals, **fields})


def _render(template: str, invoice: AttendeeInvoice, **context: t.Any) -> str:
    credit_note = AttendeeInvoiceCreditNote(
        line_items=invoice.line_items,
        amount_net=Decimal("10"),
        amount_vat=Decimal("0"),
        amount_gross=Decimal("10"),
    )
    return render_to_string(template, {"invoice": invoice, "credit_note": credit_note, **context})


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_new_exempt_sale_to_non_eu_buyer_prints_reason_not_export(
    _mail: t.Any, pdf: t.Any, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    tier = TicketTier.objects.create(
        event=event,
        name="Exempt",
        price=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
        vat_exemption_reason=REASON,
    )
    _create_payment(
        user=member_user,
        event=event,
        tier=tier,
        session_id="cs_ch",
        amount=Decimal("100.00"),
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
        buyer_billing_snapshot={**_default_billing_snapshot(), "vat_country_code": "CH", "vat_id": "CHE123456789"},
    )
    invoice = generate_attendee_invoice("cs_ch")
    assert invoice is not None
    assert invoice.buyer_vat_country == "CH"

    template, context = pdf.call_args.args
    html = render_to_string(template, context)

    assert _is_export(invoice) is False
    assert "Export of services" not in html
    assert f"VAT exempt: {REASON}" in html


def test_legacy_export_row_still_prints_export_wording() -> None:
    legacy = _doc(
        buyer_vat_country="US",
        total_vat=Decimal("0.00"),
        line_items=[_line(None, "0.00")],
    )
    assert _is_export(legacy) is True
    for template in (INVOICE, CREDIT_NOTE):
        html = _render(template, legacy, is_export=True)
        assert "Export of services" in html


@pytest.mark.parametrize("template", [INVOICE, CREDIT_NOTE])
def test_mixed_s_and_e_virtual_interim_shows_both_notices(template: str) -> None:
    invoice = _doc(
        line_items=[_line("S", "20.00"), _line("E", "0.00")],
        vat_exemptions={"E": {"text": REASON, "code": ""}},
    )
    html = _render(template, invoice, is_virtual_interim=True)
    assert "Virtual event" in html
    assert f"VAT exempt: {REASON}" in html
    assert html.count("Tax Notice") == 1


@pytest.mark.parametrize("template", [INVOICE, CREDIT_NOTE])
def test_exempt_notice_alone_gets_its_own_label(template: str) -> None:
    invoice = _doc(
        line_items=[_line("E", "0.00")],
        vat_exemptions={"E": {"text": REASON, "code": ""}},
    )
    html = _render(template, invoice)
    assert html.count("Tax Notice") == 1
    assert f"VAT exempt: {REASON}" in html


@pytest.mark.parametrize("template", [INVOICE, CREDIT_NOTE])
def test_fallback_exempt_text_is_not_prefixed(template: str) -> None:
    invoice = _doc(
        line_items=[_line("E", "0.00")],
        vat_exemptions={"E": {"text": "Exempt from VAT", "code": ""}},
    )
    assert invoice.exempt_reason_is_fallback is True
    html = _render(template, invoice)
    assert "VAT exempt: Exempt from VAT" not in html
    assert "Exempt from VAT" in html


def test_stated_reason_is_not_fallback() -> None:
    assert _doc(vat_exemptions={"E": {"text": REASON, "code": ""}}).exempt_reason_is_fallback is False
    assert _doc().exempt_reason_is_fallback is False
