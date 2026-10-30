"""A small-business-scheme seller's PDF shows no VAT rate, amount or total (#1258).

AT § 11 Abs. 12/14 UStG 1994 (VAT shown is VAT owed, by the issuer too), DE § 14c UStG, Art. 289 of
Directive 2006/112/EC. The documents show gross amounts, the seller identifier and the statutory note;
the Factur-X XML keeps its category-E breakdown at 0, which EN 16931 needs structurally.
"""

import re

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.tests.test_attendee_invoice._helpers import _create_draft
from events.tests.test_attendee_invoice.test_small_business_scheme import AT_REASON, DE_REASON, Scheme
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

VAT_COLUMN = "<th>VAT</th>"
RATE_IN_PARENS = re.compile(r"\(\d+(\.\d+)?%\)")


def _scheme_invoice(org: Organization, event: Event, user: RevelUser, scheme: str) -> AttendeeInvoice:
    invoice = _create_draft(org, event, user)
    invoice.seller_small_business_scheme = scheme
    if scheme == Scheme.DE_19:
        invoice.seller_vat_id, invoice.seller_country_code = "DE811234567", "DE"
        invoice.vat_exemptions = {"E": {"text": DE_REASON, "code": ""}}
    else:
        invoice.seller_vat_id, invoice.seller_tax_registration_number, invoice.seller_country_code = (
            "",
            "12-345/6789",
            "AT",
        )
        invoice.vat_exemptions = {"E": {"text": AT_REASON, "code": ""}}
    invoice.line_items = [{**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": "100.00"}]
    invoice.save()
    return invoice


def _render(invoice: AttendeeInvoice) -> str:
    return render_to_string(
        "invoices/attendee_invoice.html",
        {"invoice": invoice, "is_draft": False, "is_export": False, "is_virtual_interim": False},
    )


def _assert_no_vat_presentation(html: str) -> None:
    body = html.split("<body", 1)[1]
    assert VAT_COLUMN not in body
    assert "Subtotal" not in body
    assert "Reverse Charge" not in body
    assert not RATE_IN_PARENS.search(body)
    assert re.search(r"<span>VAT(\s*\(|</span>)", body) is None  # no "VAT" / "VAT (x%)" totals row
    assert "<th>Amount</th>" in body


@pytest.mark.parametrize(
    ("scheme", "identifier", "note"),
    [
        (Scheme.DE_19, "VAT: DE811234567", "VAT exempt: Steuerbefreit gemäß § 19 UStG"),
        (Scheme.AT_6_1_27, "Steuernummer: 12-345/6789", "VAT exempt: Umsatzsteuerbefreit – Kleinunternehmer"),
    ],
)
def test_scheme_invoice_shows_gross_amounts_identifier_and_note_only(
    organization: Organization, event: Event, member_user: RevelUser, scheme: str, identifier: str, note: str
) -> None:
    html = _render(_scheme_invoice(organization, event, member_user, scheme))
    _assert_no_vat_presentation(html)
    assert identifier in html  # the seller identifier line stays
    assert note in html
    assert "<span>Total</span>" in html
    assert "100.00" in html


def test_scheme_credit_note_shows_no_vat_either(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = make_invoice(
        organization,
        event,
        member_user,
        lines=[line("24.90", "0.00", "E")],
        seller_vat_id="",
        seller_tax_registration_number="12-345/6789",
        seller_country="AT",
        buyer_vat_id="",
        buyer_country="AT",
        exempt_reason=AT_REASON,
        seller_small_business_scheme=Scheme.AT_6_1_27,
    )
    note = make_credit_note(invoice, [line("24.90", "0.00", "E")])
    html = render_to_string(
        "invoices/attendee_credit_note.html",
        {"credit_note": note, "invoice": invoice, "is_export": False, "is_virtual_interim": False},
    )
    _assert_no_vat_presentation(html)
    assert "Total Credited" in html and "Steuernummer: 12-345/6789" in html
    assert "VAT exempt: Umsatzsteuerbefreit – Kleinunternehmer" in html


def test_other_sellers_keep_the_vat_breakdown(organization: Organization, event: Event, member_user: RevelUser) -> None:
    invoice = _create_draft(organization, event, member_user)  # 22%, scheme "none"
    assert invoice.seller_small_business_scheme == Scheme.NONE and not invoice.seller_is_small_business
    html = _render(invoice)
    assert VAT_COLUMN in html and "Subtotal" in html and RATE_IN_PARENS.search(html)
    assert "<th>Amount</th>" not in html
    note = make_credit_note(
        make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")]), [line("24.90", "13.00")]
    )
    cn_html = render_to_string(
        "invoices/attendee_credit_note.html",
        {"credit_note": note, "invoice": note.invoice, "is_export": False, "is_virtual_interim": False},
    )
    assert VAT_COLUMN in cn_html and "Subtotal" in cn_html
