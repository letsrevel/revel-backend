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
from euinvoice.errors import EuInvoiceError, PreflightError
from euinvoice.model import Invoice, InvoiceDraft

from common.models import SiteSettings
from events.models.attendee_invoice import AttendeeInvoice, InvoiceLineItemDict
from events.service import einvoice_mapping as mapping

logger = structlog.get_logger(__name__)

PROFILE: t.Final = profiles.FACTURX_EN16931

Document = mapping.Document
VatGroups = dict[tuple[str, Decimal | None], Decimal]

# Exceptions from the PDF stack (render and embed) that are routine failures, not bugs of ours.
_PDF_STACK: t.Final = frozenset({"weasyprint", "pypdf", "pydyf"})


class _Skipped(Exception):
    """The document gets a plain PDF; ``reason`` is the machine-readable why for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _vat_per_group(lines: list[InvoiceLineItemDict]) -> VatGroups:
    """Σ our stored line ``vat_amount`` per ``(category, rate)``, keyed as euinvoice keys BG-23."""
    groups: VatGroups = {}
    for item in lines:
        key = (item["vat_category"], mapping.line_rate(item))
        groups[key] = groups.get(key, Decimal("0.00")) + Decimal(item["vat_amount"])
    return groups


def agreement_mismatch(document: Document, invoice: Invoice, ours: VatGroups) -> str | None:
    """Return the first business term the overridden ``invoice`` still gets wrong against our figures.

    Runs after :func:`apply_our_vat`, which already made BT-117, BT-112 and BT-115 follow our VAT, so
    only what the override cannot fix is left: BT-109 must equal our net, BT-110 the sum of our line
    VAT, and BT-115 must be 0 (paid in full). The XML and the PDF come from one snapshot, so any
    difference is a bug on one side and the XML is refused.

    Returns:
        ``"BT-109"``, ``"BT-110"`` or ``"BT-115"``, or ``None`` when all agree.
    """
    net = document.total_net if isinstance(document, AttendeeInvoice) else document.amount_net
    totals = invoice.totals
    if totals.total_without_vat != net:
        return "BT-109"
    if totals.total_vat != sum(ours.values(), Decimal("0.00")):
        return "BT-110"
    if totals.amount_due != 0:
        return "BT-115"
    return None


_VAT_TOLERANCE: t.Final = Decimal("0.02")


def apply_our_vat(invoice: Invoice, ours: VatGroups) -> Invoice:
    """Return ``invoice`` with BG-23 BT-117 set to our Σ line VAT, BT-110, BT-112 and BT-115 following.

    Telescoped credit-note VAT (a difference of cumulative figures) can differ from euinvoice's
    ``round(BT-116 x rate)`` by one cent. CEN BR-CO-17 tolerates a BT-117 within less than 1 unit of
    that, so the XML carries the figures we printed rather than refusing. euinvoice's ``calc.complete``
    documents this override.

    Raises:
        _Skipped: The groups differ from ours in either direction, or one is off by 0.02 or more:
            a real bug, not rounding.
    """
    if {(g.category_code, g.rate) for g in invoice.vat_breakdown} != ours.keys():
        raise _Skipped("agreement:BT-117")
    groups = []
    for group in invoice.vat_breakdown:
        mine = ours[(group.category_code, group.rate)]
        if abs(group.tax_amount - mine) >= _VAT_TOLERANCE:
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


def _route(document: Document) -> tuple[str, Callable[[], str | None], Callable[[], InvoiceDraft]]:
    """``(kind, ineligibility reason, draft)`` for an invoice or a credit note."""
    if isinstance(document, AttendeeInvoice):
        invoice = document
        return "invoice", lambda: mapping.ineligibility_reason(invoice), lambda: mapping.invoice_draft_for(invoice)
    note = document
    return (
        "credit_note",
        lambda: mapping.credit_note_ineligibility_reason(note),
        lambda: mapping.credit_note_draft_for(note),
    )


def _xml_for(document: Document, reason_fn: Callable[[], str | None], draft_fn: Callable[[], InvoiceDraft]) -> bytes:
    """The document's EN 16931 CII XML, or :class:`_Skipped` with the reason it gets none."""
    if (reason := reason_fn()) is not None:  # pure and in memory: checked before the SiteSettings query
        raise _Skipped(reason)
    if not SiteSettings.get_solo().einvoice_facturx_enabled:
        raise _Skipped("disabled")
    ours = _vat_per_group(document.line_items)
    invoice = apply_our_vat(calc.complete(draft_fn(), **mapping.complete_kwargs_for(document)), ours)
    if (term := agreement_mismatch(document, invoice, ours)) is not None:
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
        totals and euinvoice's agree and the pre-flight passes; otherwise ``render(False)``. If only
        the embedding fails, the PDF/A-3b already rendered is returned (it is a valid plain PDF).
    """
    kind, reason_fn, draft_fn = _route(document)
    a3: bytes | None = None
    try:
        xml = _xml_for(document, reason_fn, draft_fn)
        a3 = render(True)
        return facturx.embed(a3, xml, profile=PROFILE)
    except _Skipped as skipped:
        # Routine: drafts, legacy rows and incomplete snapshots are plain by design.
        logger.info("einvoice_skipped", document_id=str(document.pk), kind=kind, reason=skipped.reason)
    except Exception as exc:  # noqa: BLE001
        # The one place a broad catch is right: "never block issuing or sending" is the anti-goal of
        # #1235. Everything means a plain PDF. Never ``str(exc)`` or a traceback: messages can echo
        # document values, so ids, the exception type and PreflightError rule ids are all we log.
        name = type(exc).__name__
        if isinstance(exc, EuInvoiceError) or type(exc).__module__.partition(".")[0] in _PDF_STACK:
            extra = {"rule_ids": [f.rule_id for f in exc.findings]} if isinstance(exc, PreflightError) else {}
            logger.warning("einvoice_skipped", document_id=str(document.pk), kind=kind, reason=f"error:{name}", **extra)
        else:  # not euinvoice, not the PDF stack: a bug in our mapping
            logger.error("einvoice_skipped", document_id=str(document.pk), kind=kind, reason=f"bug:{name}")
    return a3 if a3 is not None else render(False)
