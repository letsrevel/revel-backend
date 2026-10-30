"""Generated Factur-X invoices validate against the official CEN EN 16931 rules (#1235).

Needs the pinned validation artifacts (``python -m euinvoice artifacts fetch``); skipped without them
unless ``EINVOICE_CONFORMANCE_REQUIRED=1`` (set by the CI job), which fails instead.
Validated under ``profiles.FACTURX_EN16931`` so the euinvoice 0.3 bump adds the Factur-X Schematron
without a code change here. The documents are built with the same arithmetic generation uses
(:mod:`events.tests.test_einvoice._fixtures`) and rendered through the real PDF path.
"""

import datetime
import os
import typing as t
from collections.abc import Callable
from decimal import Decimal
from unittest.mock import patch

import pytest
from euinvoice import facturx, profiles, validate
from euinvoice.errors import ArtifactsNotAvailableError

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, TicketTier
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_draft_service import issue_draft_invoice, update_draft_invoice
from events.service.attendee_invoice_service import (
    _generate_and_save_pdf,
    _generate_credit_note_pdf,
    generate_attendee_credit_note,
    generate_attendee_invoice,
)
from events.tests.test_attendee_invoice._helpers import (
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.tests.test_einvoice._fixtures import CARD, OTHER, line, make_credit_note, make_invoice

pytestmark = [pytest.mark.django_db, pytest.mark.einvoice_conformance]


def _refund(payment: Payment, amount: str) -> Refund:
    return Refund.objects.create(
        payment=payment,
        amount=Decimal(amount),
        currency="EUR",
        status=Refund.RefundStatus.SUCCEEDED,
        source=Refund.Source.ORGANIZER_API,
    )


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


def _telescoped_credit_note(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoiceCreditNote:
    """The 2nd partial note's BT-117 (0.90) is a cent off round(BT-116 x rate) (0.89): BR-CO-17 tolerates it."""
    invoice = make_invoice(org, event, user, lines=[line("7.77", "13.00"), line("7.77", "13.00")])
    make_credit_note(invoice, [line("7.77", "13.00")])
    return make_credit_note(invoice, [line("7.77", "13.00")])


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
    "telescoped_credit_note": _telescoped_credit_note,
}


def _assert_conformant(document: AttendeeInvoice | AttendeeInvoiceCreditNote) -> None:
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
        if os.environ.get("EINVOICE_CONFORMANCE_REQUIRED") == "1":  # the CI conformance job must never skip
            pytest.fail(f"euinvoice validation artifacts missing in a job that requires them ({exc})")
        pytest.skip(f"euinvoice validation artifacts missing; run `python -m euinvoice artifacts fetch` ({exc})")
    failed = [
        f"{f.rule_id} ({f.severity}) at {f.location}: {f.message}"
        for f in report.findings
        if f.severity != "information"
    ]
    assert report.ok, "\n".join(failed)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_generated_document_is_en16931_conformant(
    name: str, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    _assert_conformant(FIXTURES[name](organization, event, member_user))


def test_credit_notes_of_a_rate_edited_draft_are_conformant(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    """#1250: a DRAFT edited from 20 % to 10 %, then partially and fully credited, validates end to end."""
    organization = _make_org_invoicing_ready(organization)
    organization.invoicing_mode = Organization.InvoicingMode.HYBRID
    organization.save()
    tier = TicketTier.objects.create(
        event=event, name="General", price=Decimal("24.90"), payment_method=TicketTier.PaymentMethod.ONLINE
    )
    payment = _create_payment(
        user=member_user,
        event=event,
        tier=tier,
        session_id="cs_conf_edit",
        amount=Decimal("24.90"),
        net_amount=Decimal("20.75"),
        vat_amount=Decimal("4.15"),
        vat_rate=Decimal("20.00"),
        buyer_billing_snapshot=_default_billing_snapshot(),
    )
    invoice = generate_attendee_invoice("cs_conf_edit")
    assert invoice is not None
    edited = {**invoice.line_items[0], "vat_rate": "10.00"}
    with patch(MOCK_SEND_EMAIL):
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"line_items": [edited]}))
    notes = [
        generate_attendee_credit_note("cs_conf_edit", [payment.id], refund_ids=[row.id])
        for row in (_refund(payment, "10.00"), _refund(payment, "14.90"))
    ]
    assert all(notes)
    _assert_conformant(invoice)
    for note in notes:
        assert note is not None
        assert [li["vat_rate"] for li in note.line_items] == ["10.00"]
        _assert_conformant(note)
