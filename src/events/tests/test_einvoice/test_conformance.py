"""Generated Factur-X invoices validate against the official CEN EN 16931 rules (#1235).

Needs the pinned validation artifacts (``python -m euinvoice artifacts fetch``); skipped without them.
Validated under ``profiles.FACTURX_EN16931`` so the euinvoice 0.3 bump adds the Factur-X Schematron
without a code change here. The documents are built with the same arithmetic generation uses
(:mod:`events.tests.test_einvoice._fixtures`) and rendered through the real PDF path.
"""

import datetime
import typing as t
from collections.abc import Callable

import pytest
from euinvoice import facturx, profiles, validate
from euinvoice.errors import ArtifactsNotAvailableError

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import _generate_and_save_pdf, _generate_credit_note_pdf
from events.tests.test_einvoice._fixtures import CARD, OTHER, line, make_credit_note, make_invoice

pytestmark = [pytest.mark.django_db, pytest.mark.einvoice_conformance]

Build = Callable[[Organization, Event, RevelUser], AttendeeInvoice | AttendeeInvoiceCreditNote]

_MIXED = [line("24.90", "13.00"), line("50.00", "20.00", description="VIP"), line("10.00", "0.00", "E", "Workshop")]


def _mixed_with_rounding(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """Review focus 5: S at two rates plus E, and 24.90 at 13 % books a -0.01 rounding amount."""
    invoice = make_invoice(org, event, user, lines=_MIXED, exempt_reason="Art. 132(1)(n) VAT Directive")
    assert invoice.rounding_amount != 0
    return invoice


def _full_credit_note(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoiceCreditNote:
    invoice = make_invoice(org, event, user, lines=_MIXED, exempt_reason="Art. 132(1)(n) VAT Directive")
    note = make_credit_note(
        invoice, [line(li["unit_price_gross"], li["vat_rate"], li["vat_category"]) for li in _MIXED]
    )
    invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
    invoice.save()
    return note


def _partial_credit_note(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoiceCreditNote:
    """Review focus 3: one of two 24.90 tickets refunded."""
    invoice = make_invoice(org, event, user, lines=[line("24.90", "13.00"), line("24.90", "13.00")])
    return make_credit_note(invoice, [line("24.90", "13.00", description="Refund — Event — Tier — Guest")])


FIXTURES: dict[str, Build] = {
    "single_rate": lambda org, event, user: make_invoice(org, event, user, lines=[line("24.90", "13.00")]),
    "mixed_s_e_with_rounding": _mixed_with_rounding,
    "reverse_charge": lambda org, event, user: make_invoice(
        org, event, user, lines=[line("100.00", "0.00", "AE")], reverse_charge=True, buyer_country="AT"
    ),
    "out_of_scope": lambda org, event, user: make_invoice(
        org, event, user, lines=[line("100.00", "0.00", "O")], buyer_country="US", buyer_vat_id=""
    ),
    "supply_period": lambda org, event, user: make_invoice(
        org, event, user, lines=[line("24.90", "13.00")], supply_period_end=datetime.date(2026, 3, 16)
    ),
    "card_payment": lambda org, event, user: make_invoice(
        org, event, user, lines=[line("24.90", "13.00")], payment=CARD
    ),
    "other_payment": lambda org, event, user: make_invoice(
        org, event, user, lines=[line("24.90", "13.00")], payment=OTHER
    ),
    "full_credit_note": _full_credit_note,
    "partial_credit_note": _partial_credit_note,
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_generated_document_is_en16931_conformant(
    name: str, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    document = FIXTURES[name](organization, event, member_user)
    if isinstance(document, AttendeeInvoice):
        _generate_and_save_pdf(document)
    else:
        _generate_credit_note_pdf(document)
    document.refresh_from_db()
    with document.pdf_file.open("rb") as handle:
        pdf = t.cast(bytes, handle.read())
    extracted = facturx.extract(pdf)  # a plain fallback PDF has no invoice attachment and fails here
    assert extracted.conformance_level == "EN 16931"
    try:
        report = validate(extracted.xml, profiles.FACTURX_EN16931)
    except ArtifactsNotAvailableError as exc:
        pytest.skip(f"euinvoice validation artifacts missing; run `python -m euinvoice artifacts fetch` ({exc})")
    failed = [
        f"{f.rule_id} ({f.severity}) at {f.location}: {f.message}"
        for f in report.findings
        if f.severity != "information"
    ]
    assert report.ok, "\n".join(failed)
