from django.core.validators import MinValueValidator
from django.db import models

from common.fields import ProtectedFileField
from common.models import TimeStampedModel


class PlatformFeeInvoice(TimeStampedModel):
    """Monthly platform fee invoice for an organization.

    Snapshots org and platform details at generation time for historical accuracy,
    even if the organization is later deleted or changes details.
    """

    class InvoiceStatus(models.TextChoices):
        DRAFT = "draft"
        ISSUED = "issued"
        PAID = "paid"
        CANCELLED = "cancelled"

    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.SET_NULL,
        null=True,
        related_name="platform_fee_invoices",
    )
    invoice_number = models.CharField(
        max_length=30,
        unique=True,
        help_text="Sequential invoice number (e.g., RVL-2024-001234).",
    )
    period_start = models.DateField(help_text="First day of the invoiced period.")
    period_end = models.DateField(help_text="Last day of the invoiced period.")

    # Fee breakdown
    fee_gross = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)], help_text="Total fee (VAT-inclusive)."
    )
    fee_net = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)], help_text="Fee excluding VAT."
    )
    fee_vat = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)], help_text="VAT portion of the fee."
    )
    fee_vat_rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="VAT rate applied to this invoice.")
    currency = models.CharField(max_length=3, default="EUR", help_text="ISO 4217 currency code.")
    reverse_charge = models.BooleanField(
        default=False, help_text="Whether reverse charge applies (EU B2B cross-border)."
    )

    # Organization snapshot
    org_name = models.CharField(max_length=255)
    org_vat_id = models.CharField(max_length=20, blank=True, default="")
    org_vat_country = models.CharField(max_length=2, blank=True, default="")
    org_address = models.TextField(blank=True, default="")

    # Platform snapshot
    platform_business_name = models.CharField(max_length=255)
    platform_business_address = models.TextField()
    platform_vat_id = models.CharField(max_length=20)

    # Aggregate stats
    total_tickets = models.PositiveIntegerField(default=0, help_text="Number of tickets sold in this period.")
    total_ticket_revenue = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Total ticket revenue in this period."
    )

    # Status & delivery
    status = models.CharField(max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True, help_text="When the invoice was issued.")
    pdf_file = ProtectedFileField(upload_to="invoices/platform_fee/", null=True, blank=True)

    class Meta:
        ordering = ["-period_start", "org_name"]
        indexes = [
            models.Index(fields=["organization", "period_start"], name="idx_invoice_org_period"),
            models.Index(fields=["status"], name="idx_invoice_status"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "period_start", "currency"],
                name="unique_invoice_per_org_period_currency",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_number} ({self.org_name}, {self.period_start:%Y-%m})"


class PlatformFeeCreditNote(TimeStampedModel):
    """Credit note for refunds on already-invoiced platform fee payments."""

    invoice = models.ForeignKey(
        PlatformFeeInvoice,
        on_delete=models.PROTECT,
        related_name="credit_notes",
    )
    credit_note_number = models.CharField(
        max_length=30,
        unique=True,
        help_text="Sequential credit note number (e.g., RVL-CN-2024-001234).",
    )

    # Refunded fee breakdown
    fee_gross = models.DecimalField(max_digits=10, decimal_places=2, help_text="Refunded fee (VAT-inclusive).")
    fee_net = models.DecimalField(max_digits=10, decimal_places=2, help_text="Refunded fee excluding VAT.")
    fee_vat = models.DecimalField(max_digits=10, decimal_places=2, help_text="Refunded VAT portion.")

    issued_at = models.DateTimeField(null=True, blank=True, help_text="When the credit note was issued.")
    pdf_file = ProtectedFileField(upload_to="invoices/credit_notes/", null=True, blank=True)

    # Link to the refunded payment for traceability
    payment = models.ForeignKey(
        "events.Payment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_notes",
        help_text="The refunded payment that triggered this credit note.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.credit_note_number} (for {self.invoice.invoice_number})"
