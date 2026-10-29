"""Factur-X / ZUGFeRD EN 16931 PDFs for attendee invoices and credit notes (#1235).

:func:`facturx_pdf_for` is the one boundary between invoicing and euinvoice: it maps the document
(:mod:`events.service.einvoice_mapping`), lets euinvoice compute the totals, refuses the result when
euinvoice's figures differ from ours, runs the pre-flight checks through ``to_xml`` and embeds the CII
XML into a PDF/A-3 render. Anything that goes wrong on that path, euinvoice exception or not, means
the plain PDF of today: issuing and sending an invoice is never blocked by e-invoicing.
"""

import typing as t
from collections.abc import Callable
from decimal import Decimal

import structlog
from euinvoice import calc, facturx, profiles, to_xml
from euinvoice.errors import PreflightError
from euinvoice.model import Invoice

from common.models import SiteSettings
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict
from events.service import einvoice_mapping as mapping

logger = structlog.get_logger(__name__)

PROFILE: t.Final = profiles.FACTURX_EN16931

Document = AttendeeInvoice | AttendeeInvoiceCreditNote


class _Skipped(Exception):
    """The document gets a plain PDF; ``reason`` is the machine-readable why for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _vat_per_group(lines: list[InvoiceLineItemDict]) -> dict[tuple[str, Decimal | None], Decimal]:
    """Σ our stored line ``vat_amount`` per ``(category, rate)``, keyed as euinvoice keys BG-23."""
    groups: dict[tuple[str, Decimal | None], Decimal] = {}
    for item in lines:
        key = (item["vat_category"], mapping.line_rate(item))
        groups[key] = groups.get(key, Decimal("0.00")) + Decimal(item["vat_amount"])
    return groups


def agreement_mismatch(document: Document, invoice: Invoice) -> str | None:
    """Return the first business term where euinvoice's arithmetic differs from our stored totals.

    Runs after :func:`apply_our_vat`, so BT-117 and the terms derived from it already carry our figures;
    BT-109 must equal our net and BT-115 must be 0 (paid in full). The XML and the PDF come from one
    snapshot, so any difference is a bug on one side and the XML is refused.

    Returns:
        ``"BT-117"``, ``"BT-109"``, ``"BT-110"``, ``"BT-112"`` or ``"BT-115"``, or ``None`` when all agree.
    """
    if isinstance(document, AttendeeInvoice):
        net, vat = document.total_net, document.total_vat
    else:
        net, vat = document.amount_net, document.amount_vat
    ours = _vat_per_group(document.line_items)
    for group in invoice.vat_breakdown:
        if group.tax_amount != ours.get((group.category_code, group.rate)):
            return "BT-117"
    totals = invoice.totals
    if totals.total_without_vat != net:
        return "BT-109"
    if totals.total_vat != vat:
        return "BT-110"
    if totals.total_with_vat != net + vat:
        return "BT-112"
    if totals.amount_due != 0:
        return "BT-115"
    return None


_VAT_TOLERANCE: t.Final = Decimal("0.02")


def apply_our_vat(document: Document, invoice: Invoice) -> Invoice:
    """Return ``invoice`` with BG-23 BT-117 set to our Σ line VAT, BT-110, BT-112 and BT-115 following.

    Telescoped credit-note VAT (a difference of cumulative figures) can differ from euinvoice's
    ``round(BT-116 x rate)`` by one cent. CEN BR-CO-17 tolerates a BT-117 within less than 1 unit of
    that, so the XML carries the figures we printed rather than refusing. euinvoice's ``calc.complete``
    documents this override.

    Raises:
        _Skipped: A group differs from ours by 0.02 or more (or is unknown to us): a real bug, not rounding.
    """
    ours = _vat_per_group(document.line_items)
    groups = []
    for group in invoice.vat_breakdown:
        mine = ours.get((group.category_code, group.rate))
        if mine is None or abs(group.tax_amount - mine) >= _VAT_TOLERANCE:
            raise _Skipped("agreement:BT-117")
        groups.append(group.model_copy(update={"tax_amount": mine}))
    totals = invoice.totals
    total_vat = sum((g.tax_amount for g in groups), Decimal("0.00"))
    total_with_vat = totals.total_without_vat + total_vat
    amount_due = total_with_vat - (totals.paid_amount or Decimal("0.00")) + (totals.rounding_amount or Decimal("0.00"))
    new_totals = totals.model_copy(
        update={"total_vat": total_vat, "total_with_vat": total_with_vat, "amount_due": amount_due}
    )
    return Invoice.model_validate({**dict(invoice), "vat_breakdown": tuple(groups), "totals": new_totals})


def _xml_for(document: Document) -> bytes:
    """The document's EN 16931 CII XML, or :class:`_Skipped` with the reason it gets none."""
    if not SiteSettings.get_solo().einvoice_facturx_enabled:
        raise _Skipped("disabled")
    if isinstance(document, AttendeeInvoice):
        reason = mapping.ineligibility_reason(document)
    else:
        reason = mapping.credit_note_ineligibility_reason(document)
    if reason is not None:
        raise _Skipped(reason)
    draft = (
        mapping.invoice_draft_for(document)
        if isinstance(document, AttendeeInvoice)
        else mapping.credit_note_draft_for(document)
    )
    invoice = apply_our_vat(document, calc.complete(draft, **mapping.complete_kwargs_for(document)))
    if (term := agreement_mismatch(document, invoice)) is not None:
        raise _Skipped(f"agreement:{term}")
    # Runs the profile pre-flight and calc.check; a fatal finding raises PreflightError.
    return to_xml(invoice, profile=PROFILE)


def facturx_pdf_for(document: Document, render: Callable[[bool], bytes]) -> bytes:
    """Render ``document`` as a Factur-X EN 16931 PDF when it qualifies, else as the plain PDF.

    Args:
        document: An attendee invoice or credit note, already saved.
        render: ``render(pdf_a3)`` renders the document's PDF; ``True`` asks for PDF/A-3b.

    Returns:
        ``facturx.embed(render(True), xml)`` when the kill switch is on, the document is eligible, our
        totals and euinvoice's agree and the pre-flight passes; otherwise ``render(False)``.
    """
    kind = "invoice" if isinstance(document, AttendeeInvoice) else "credit_note"
    try:
        xml = _xml_for(document)
        return facturx.embed(render(True), xml, profile=PROFILE)
    except _Skipped as skipped:
        # Routine: drafts, legacy rows and incomplete snapshots are plain by design.
        logger.info("einvoice_skipped", document_id=str(document.pk), kind=kind, reason=skipped.reason)
    except Exception as exc:  # noqa: BLE001
        # The one place a broad catch is right: "never block issuing or sending" is the anti-goal of
        # #1235. euinvoice raises PreflightError, ModelError, PdfError and, on hostile input, whatever
        # pypdf raises; the PDF/A-3 render itself can fail too. Each one means the plain PDF, logged
        # with ids only so the bug can be chased without exposing the document.
        # Rule ids (never ``str(exc)``, which can echo document values) are what lets a PreflightError be chased.
        extra = {"rule_ids": [f.rule_id for f in exc.findings]} if isinstance(exc, PreflightError) else {}
        logger.warning(
            "einvoice_skipped", document_id=str(document.pk), kind=kind, reason=f"error:{type(exc).__name__}", **extra
        )
    return render(False)
