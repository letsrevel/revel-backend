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
from events.service import einvoice_service
from events.service.attendee_invoice_service import (
    _generate_and_save_pdf,
    _generate_credit_note_pdf,
    deliver_attendee_invoice,
    deliver_credit_note,
)
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

    def test_net_mismatch_falls_back(self, invoice: AttendeeInvoice) -> None:
        """A stored header net that differs from euinvoice's BT-109 refuses the XML; the log carries ids only."""
        invoice.total_net += Decimal("0.01")
        invoice.save()
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]
        (entry,) = logs
        assert entry["reason"] == "agreement:BT-109"
        assert entry["kind"] == "invoice"
        assert set(entry) == {"event", "log_level", "document_id", "kind", "reason"}

    def test_vat_header_drift_falls_back(self, invoice: AttendeeInvoice) -> None:
        """A printed VAT header that drifted from the lines (raw update) refuses the XML: PDF and XML must agree."""
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(total_vat=invoice.total_vat + Decimal("0.01"))
        invoice.refresh_from_db()
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]
        assert logs[0]["reason"] == "agreement:BT-110"

    def test_vat_two_cents_off_falls_back(self, invoice: AttendeeInvoice) -> None:
        """BR-CO-17 forgives a cent of rounding, not two: that is a bug on one side."""
        invoice.line_items[0]["vat_amount"] = str(Decimal(invoice.line_items[0]["vat_amount"]) + Decimal("0.02"))
        invoice.save()
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]
        assert logs[0]["reason"] == "agreement:BT-117"

    def test_telescoped_credit_note_vat_one_cent_off_is_written(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """The 2nd partial note's telescoped VAT is 0.90 where round(6.87 x 13 %) is 0.89: our figures win."""
        invoice = make_invoice(organization, event, member_user, lines=[line("7.77", "13.00"), line("7.77", "13.00")])
        make_credit_note(invoice, [line("7.77", "13.00")])
        note = make_credit_note(invoice, [line("7.77", "13.00")])
        assert note.amount_vat == Decimal("0.90")
        _generate_credit_note_pdf(note)
        parsed = parse(_saved(note))
        (group,) = parsed.vat_breakdown
        assert group.tax_amount == note.amount_vat == parsed.totals.total_vat
        assert parsed.totals.total_without_vat == note.amount_net
        assert parsed.totals.total_with_vat == note.amount_net + note.amount_vat
        assert parsed.totals.amount_due == 0

    def test_preflight_error_falls_back(self, invoice: AttendeeInvoice) -> None:
        """Review focus 2: euinvoice refusing the XML still leaves a saved plain PDF."""
        with patch("events.service.einvoice_service.to_xml", side_effect=_PREFLIGHT), capture_logs() as logs:
            _generate_and_save_pdf(invoice)
        _is_plain(_saved(invoice))
        assert logs[0]["reason"] == "error:PreflightError"
        assert logs[0]["log_level"] == "warning"
        assert logs[0]["rule_ids"] == ["BR-X"]
        assert "boom" not in str(logs)

    def test_embed_error_returns_the_rendered_pdf_a3(self, invoice: AttendeeInvoice) -> None:
        """A failed embed returns the PDF/A-3b already rendered (a valid plain PDF) instead of rendering twice."""
        with (
            patch("events.service.einvoice_service.facturx.embed", side_effect=PdfError("boom")),
            capture_logs() as logs,
        ):
            render = _SpyRender(a3_pdf=b"%PDF-a3")
            assert facturx_pdf_for(invoice, render) == b"%PDF-a3"
        assert render.calls == [True]
        (entry,) = logs
        assert entry["event"] == "einvoice_skipped"
        assert entry["reason"] == "error:PdfError"
        assert entry["log_level"] == "warning"
        assert "boom" not in str(logs)

    def test_xml_error_renders_the_plain_pdf(self, invoice: AttendeeInvoice) -> None:
        with patch("events.service.einvoice_service.to_xml", side_effect=_PREFLIGHT):
            render = _SpyRender(a3_pdf=b"%PDF-a3")
            assert facturx_pdf_for(invoice, render) == PLAIN
        assert render.calls == [False]

    def test_pdf_stack_error_is_a_warning(self, invoice: AttendeeInvoice) -> None:
        class WeasyError(Exception):
            __module__ = "weasyprint.errors"

        def boom(pdf_a3: bool) -> bytes:
            if pdf_a3:
                raise WeasyError("secret")
            return PLAIN

        with capture_logs() as logs:
            assert facturx_pdf_for(invoice, boom) == PLAIN
        assert logs[0]["log_level"] == "warning"
        assert logs[0]["reason"] == "error:WeasyError"

    def test_unexpected_exception_is_logged_as_a_bug(self, invoice: AttendeeInvoice) -> None:
        """Anything that is neither euinvoice nor the PDF stack is a bug of ours: error level, no message."""
        with (
            patch("events.service.einvoice_mapping.invoice_draft_for", side_effect=KeyError("secret-value")),
            capture_logs() as logs,
        ):
            assert facturx_pdf_for(invoice, _SpyRender()) == PLAIN
        (entry,) = logs
        assert entry["log_level"] == "error"
        assert entry["reason"] == "bug:KeyError"
        assert "secret-value" not in str(logs)
        assert "exc_info" not in entry

    def test_blank_names_stay_plain_without_error_logs(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        for field, reason in (("seller_name", "missing_seller_name"), ("buyer_name", "missing_buyer_name")):
            overrides: dict[str, t.Any] = {field: "  "}
            doc = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], **overrides)
            with capture_logs() as logs:
                assert facturx_pdf_for(doc, _SpyRender()) == PLAIN
            assert logs[0]["log_level"] == "info"
            assert logs[0]["reason"] == reason

    def test_group_missing_from_euinvoice_breakdown_falls_back(self, invoice: AttendeeInvoice) -> None:
        """A (category, rate) group we hold that euinvoice's BG-23 lacks is a mismatch, not a silent drop."""
        real = einvoice_service._vat_per_group
        extra: dict[tuple[str, Decimal | None], Decimal] = {("S", Decimal("7.00")): Decimal("1.00")}
        with (
            patch.object(einvoice_service, "_vat_per_group", lambda lines: {**real(lines), **extra}),
            capture_logs() as logs,
        ):
            assert facturx_pdf_for(invoice, _SpyRender()) == PLAIN
        assert logs[0]["reason"] == "agreement:BT-117"

    def test_ineligible_document_does_not_query_site_settings(
        self, organization: Organization, event: Event, member_user: RevelUser, django_assert_num_queries: t.Any
    ) -> None:
        draft = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00")], status=AttendeeInvoice.InvoiceStatus.DRAFT
        )
        with django_assert_num_queries(0):
            assert facturx_pdf_for(draft, _SpyRender()) == PLAIN

    def test_out_of_scope_mixed_stays_plain(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        mixed = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00"), line("100.00", "0.00", "O")],
            buyer_country="US",
            buyer_vat_id="",
        )
        render = _SpyRender()
        with capture_logs() as logs:
            assert facturx_pdf_for(mixed, render) == PLAIN
        assert render.calls == [False]
        assert logs[0]["reason"] == "out_of_scope_mixed"
        assert logs[0]["log_level"] == "info"

    def test_cancelled_invoice_rerenders_as_facturx(self, invoice: AttendeeInvoice) -> None:
        """A full credit note cancels the invoice; ensure_pdf_exists on it must still embed the XML."""
        invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
        invoice.save()
        _generate_and_save_pdf(invoice)
        assert facturx.extract(_saved(invoice)).conformance_level == "EN 16931"

    def test_invoice_still_delivered_after_forced_fallback(self, invoice: AttendeeInvoice) -> None:
        """Review focus 2: a PreflightError never blocks delivery; the plain PDF is what gets emailed."""
        with (
            patch("events.service.einvoice_service.to_xml", side_effect=_PREFLIGHT),
            patch("common.tasks.send_email") as send,
        ):
            deliver_attendee_invoice(invoice)
        send.assert_called_once()
        invoice.refresh_from_db()
        assert invoice.email_sent_at is not None
        _is_plain(_saved(invoice))

    def test_credit_note_still_generated_and_delivered_after_forced_fallback(self, invoice: AttendeeInvoice) -> None:
        note = make_credit_note(invoice, [line("24.90", "13.00")])
        with (
            patch("events.service.einvoice_service.to_xml", side_effect=_PREFLIGHT),
            patch("common.tasks.send_email") as send,
        ):
            deliver_credit_note(note)
        send.assert_called_once()
        note.refresh_from_db()
        assert note.email_sent_at is not None
        _is_plain(_saved(note))

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
