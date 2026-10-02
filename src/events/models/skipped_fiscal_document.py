"""Fiscal documents Revel did not issue because a country policy refused them (#1091).

Since EU layer 1 (#1069) an attendee invoice or credit note is skipped when the
policy of a country reaching the sale refuses Revel-issued documents: B2B
e-invoicing mandates (BE Peppol, PL KSeF) and fiscalized invoicing (HR, PT, RO,
SI, GR, HU, the Basque Country, and the rest of ES from 2027). The organizer must then issue the document from
its own compliant system; these rows tell it which sales those are.

Snapshotted when the skip is decided; only the resolution fields change afterwards.
The decision depends on today's date (``in_force``), billing data can change, and
``Payment`` rows cascade with their user, so nothing here can be recomputed later.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel
from events.models.attendee_invoice import InvoiceLineItemDict, InvoiceVatBucketDict, vat_breakdown_of


class SkippedFiscalDocumentKind(models.TextChoices):
    """Which document was skipped. Module-level, distinct name: see ``AttendeeInvoiceStatus`` (#782)."""

    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"


class SkippedFiscalDocumentReason(models.TextChoices):
    """Why the policy refused; equal to the ``Decision.code`` constants in ``events.compliance.base``."""

    B2B_E_INVOICING = "b2b_e_invoicing"
    FISCALIZED_INVOICING = "fiscalized_invoicing"


class SkippedFiscalDocument(TimeStampedModel):
    """An attendee invoice or credit note Revel skipped, for the organizer to issue itself."""

    Kind = SkippedFiscalDocumentKind
    ReasonCode = SkippedFiscalDocumentReason

    kind = models.CharField(max_length=20, choices=Kind.choices)
    reason_code = models.CharField(max_length=30, choices=ReasonCode.choices)
    policy_country = models.CharField(max_length=2, help_text="The country whose policy refused the document.")
    reason = models.TextField(help_text="The policy's explanation, in the language active when it was decided.")

    organization = models.ForeignKey(
        "events.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="skipped_fiscal_documents"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="skipped_fiscal_documents"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="skipped_fiscal_documents",
    )
    stripe_session_id = models.CharField(max_length=255, db_index=True)
    # A skipped credit note points at the invoice it would have corrected: the Revel
    # invoice (``invoice``), or the skipped invoice record (``parent``).
    invoice = models.ForeignKey(
        "events.AttendeeInvoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="skipped_credit_notes"
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="skipped_credit_notes"
    )
    payments = models.ManyToManyField("events.Payment", blank=True, related_name="skipped_fiscal_documents")
    refunds = models.ManyToManyField("events.Refund", blank=True, related_name="skipped_fiscal_documents")

    # Buyer snapshot
    buyer_name = models.CharField(max_length=255, blank=True, default="")
    buyer_email = models.EmailField(blank=True, default="")
    buyer_vat_id = models.CharField(max_length=20, blank=True, default="")
    buyer_vat_country = models.CharField(max_length=2, blank=True, default="")
    buyer_address = models.TextField(blank=True, default="")
    # The checkout VIES outcome (a ``VatIdStatus`` value); empty when unknown.
    buyer_vat_id_status = models.CharField(max_length=20, blank=True, default="")

    # Amounts, as the skipped document would have stated them (positive on credit notes).
    currency = models.CharField(max_length=3)
    total_gross = models.DecimalField(max_digits=10, decimal_places=2)
    total_net = models.DecimalField(max_digits=10, decimal_places=2)
    total_vat = models.DecimalField(max_digits=10, decimal_places=2)
    line_items = models.JSONField(default=list, blank=True)

    decided_at = models.DateTimeField(default=timezone.now)
    notified_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_fiscal_documents",
    )
    external_reference = models.CharField(
        max_length=255, blank=True, default="", help_text="The document number in the organizer's own system."
    )

    class Meta:
        ordering = ["-decided_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_session_id"],
                condition=models.Q(kind="invoice"),
                name="unique_skipped_invoice_per_session",
            ),
        ]
        indexes = [models.Index(fields=["organization", "decided_at"])]

    @property
    def vat_breakdown(self) -> list[InvoiceVatBucketDict]:
        """The line items grouped by VAT rate, as on an attendee invoice."""
        items: list[InvoiceLineItemDict] = self.line_items
        return vat_breakdown_of(items)

    def __str__(self) -> str:
        return f"Skipped {self.kind} {self.total_gross} {self.currency} ({self.stripe_session_id})"
