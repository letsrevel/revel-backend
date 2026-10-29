"""Factur-X embedding, the agreement check and every fallback to the plain PDF (#1235)."""

import typing as t
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from euinvoice import facturx, parse
from euinvoice.errors import PdfError, PreflightError
from euinvoice.report import Finding, Severity
from pypdf import PdfReader
from structlog.testing import capture_logs

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, TotalsMethod
from events.service.attendee_invoice_service import _generate_and_save_pdf, _generate_credit_note_pdf
from events.service.einvoice_service import facturx_pdf_for
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

PLAIN = b"%PDF-plain"
_PREFLIGHT = PreflightError(
    "facturx-en16931",
    "cii",
    (Finding(rule_id="BR-X", severity=Severity.FATAL, location=None, message="x", source="calc"),),
)


class _SpyRender:
    """Records the ``pdf_a3`` flags asked for; a plain render is the sentinel, an A-3 one a real PDF."""

    def __init__(self, a3_pdf: bytes = b"") -> None:
        self.calls: list[bool] = []
        self.a3_pdf = a3_pdf

    def __call__(self, pdf_a3: bool) -> bytes:
        self.calls.append(pdf_a3)
        return self.a3_pdf if pdf_a3 else PLAIN


def _saved(document: AttendeeInvoice | AttendeeInvoiceCreditNote) -> bytes:
    document.refresh_from_db()
    assert document.pdf_file
    with document.pdf_file.open("rb") as handle:
        return t.cast(bytes, handle.read())


def _is_plain(pdf: bytes) -> None:
    reader = PdfReader(BytesIO(pdf))
    assert not reader.attachments
    assert reader.xmp_metadata is None or reader.xmp_metadata.pdfaid_part is None
    with pytest.raises(PdfError):
        facturx.extract(pdf)


@pytest.fixture
def invoice(organization: Organization, event: Event, member_user: RevelUser) -> AttendeeInvoice:
    return make_invoice(organization, event, member_user, lines=[line("24.90", "13.00"), line("50.00", "20.00")])


class TestEligible:
    def test_invoice_gets_facturx_pdf(self, invoice: AttendeeInvoice) -> None:
        _generate_and_save_pdf(invoice)
        pdf = _saved(invoice)
        found = facturx.extract(pdf)
        assert found.conformance_level == "EN 16931"
        assert found.filename == "factur-x.xml"
        assert PdfReader(BytesIO(pdf)).xmp_metadata.pdfaid_part == "3"  # type: ignore[union-attr]
        parsed = parse(found.xml)
        assert parsed.number == invoice.invoice_number
        assert parsed.type_code == "380"
        assert parsed.totals.total_without_vat == invoice.total_net
        assert parsed.totals.total_vat == invoice.total_vat
        assert parsed.totals.paid_amount == invoice.total_gross
        assert parsed.totals.rounding_amount == invoice.rounding_amount
        assert len(parsed.lines) == 2

    def test_credit_note_gets_facturx_pdf(self, invoice: AttendeeInvoice) -> None:
        """Review focus 3: a partial credit note is a type 381 document with its own totals."""
        note = make_credit_note(invoice, [line("24.90", "13.00", description="Refund — Event — Tier — Guest")])
        _generate_credit_note_pdf(note)
        parsed = parse(_saved(note))
        assert parsed.type_code == "381"
        assert parsed.preceding_invoice_references[0].reference == invoice.invoice_number
        assert parsed.totals.total_without_vat == note.amount_net == Decimal("22.04")
        assert parsed.totals.paid_amount == note.amount_gross == Decimal("24.90")


class TestPlainFallback:
    def test_legacy_stays_plain(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        legacy = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00")], totals_method=TotalsMethod.LEGACY
        )
        render = _SpyRender()
        assert facturx_pdf_for(legacy, render) == PLAIN
        assert render.calls == [False]

    def test_draft_stays_plain(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        draft = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00")], status=AttendeeInvoice.InvoiceStatus.DRAFT
        )
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(draft, render) == PLAIN
        assert render.calls == [False]
        assert logs == [
            {
                "event": "einvoice_skipped",
                "log_level": "info",
                "document_id": str(draft.pk),
                "kind": "invoice",
                "reason": "draft",
            }
        ]

    def test_missing_seller_vat_id_stays_plain(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], seller_vat_id="")
        _generate_and_save_pdf(invoice)
        _is_plain(_saved(invoice))

    def test_agreement_mismatch_falls_back(self, invoice: AttendeeInvoice) -> None:
        """A stored line VAT a cent off euinvoice's BT-117 refuses the XML; the log carries ids only."""
        invoice.line_items[0]["vat_amount"] = str(Decimal(invoice.line_items[0]["vat_amount"]) + Decimal("0.01"))
        invoice.save()
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]
        (entry,) = logs
        assert entry["reason"] == "agreement:BT-117"
        assert entry["kind"] == "invoice"
        assert set(entry) == {"event", "log_level", "document_id", "kind", "reason"}

    def test_preflight_error_falls_back(self, invoice: AttendeeInvoice) -> None:
        """Review focus 2: euinvoice refusing the XML still leaves a saved plain PDF."""
        with patch("events.service.einvoice_service.to_xml", side_effect=_PREFLIGHT), capture_logs() as logs:
            _generate_and_save_pdf(invoice)
        _is_plain(_saved(invoice))
        assert logs[0]["reason"] == "error:PreflightError"
        assert logs[0]["log_level"] == "warning"

    def test_embed_error_falls_back(self, invoice: AttendeeInvoice) -> None:
        with patch("events.service.einvoice_service.facturx.embed", side_effect=RuntimeError("boom")):
            render = _SpyRender(a3_pdf=b"%PDF-a3")
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [True, False]

    def test_kill_switch_off(self, invoice: AttendeeInvoice) -> None:
        """Review focus 4."""
        settings = SiteSettings.get_solo()
        settings.einvoice_facturx_enabled = False
        settings.save()
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]
        assert logs[0]["reason"] == "disabled"

    def test_legacy_credit_note_stays_plain(self, invoice: AttendeeInvoice) -> None:
        note = make_credit_note(invoice, [line("24.90", "13.00")], totals_method=TotalsMethod.LEGACY)
        render = _SpyRender()
        assert facturx_pdf_for(note, render) == PLAIN
        assert render.calls == [False]
