"""Attendee invoice and credit note models.

Invoices issued to attendees (buyers) on behalf of organizers (sellers)
for online ticket purchases. The organizer is the legal seller; Revel acts
as an intermediary generating and delivering invoices.
"""

import typing as t

from django.conf import settings
from django.db import models

from common.fields import ProtectedFileField
from common.models import EmailDeliverableMixin, TimeStampedModel


class BuyerBillingSnapshot(t.TypedDict):
    """Typed structure for Payment.buyer_billing_snapshot."""

    billing_name: str
    vat_id: str
    vat_country_code: str
    vat_id_validated: bool
    billing_address: str
    billing_email: str
    reverse_charge: bool


class InvoiceLineItemDict(t.TypedDict):
    """Typed structure for line items stored in AttendeeInvoice.line_items JSON."""

    description: str
    unit_price_gross: str
    discount_amount: str
    net_amount: str
    vat_amount: str
    vat_rate: str


class AttendeeInvoiceStatus(models.TextChoices):
    """Status of an :class:`AttendeeInvoice`.

    Module-level with a distinct class name (not nested as ``InvoiceStatus``):
    django-ninja dedupes OpenAPI ``components.schemas`` by bare class name with
    last-writer-wins, so this 3-value enum and ``PlatformFeeInvoice``'s 4-value
    ``InvoiceStatus`` silently clobbered each other in the generated spec
    (#782). Code keeps using the ``AttendeeInvoice.InvoiceStatus`` idiom via
    the class-level alias.
    """

    DRAFT = "draft"
    ISSUED = "issued"
    CANCELLED = "cancelled"


class AttendeeInvoice(EmailDeliverableMixin, TimeStampedModel):
    """Invoice issued to an attendee on behalf of an organizer.

    In HYBRID mode, invoices start as DRAFT and can be edited by the org admin
    before being manually issued. In AUTO mode, invoices are created as ISSUED
    and sent immediately.

    All fields except seller (org) info are editable while in DRAFT status.
    Once ISSUED, the invoice is immutable (can only be cancelled via credit note).
    """

    # Alias so callers keep the ``Model.InvoiceStatus`` idiom; the class lives
    # at module level under a collision-free name (see its docstring, #782).
    InvoiceStatus = AttendeeInvoiceStatus

    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.SET_NULL,
        null=True,
        related_name="attendee_invoices",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="attendee_invoices",
    )
    stripe_session_id = models.CharField(max_length=255, db_index=True)

    invoice_number = models.CharField(max_length=50, unique=True)
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
    )

    # Totals (initially from Payments, fully editable in DRAFT)
    total_gross = models.DecimalField(max_digits=10, decimal_places=2)
    total_net = models.DecimalField(max_digits=10, decimal_places=2)
    total_vat = models.DecimalField(max_digits=10, decimal_places=2)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    currency = models.CharField(max_length=3)
    reverse_charge = models.BooleanField(default=False)

    # Discount
    discount_code_text = models.CharField(max_length=64, blank=True, default="")
    discount_amount_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Line items (JSON snapshot, editable in DRAFT)
    # Structure per item:
    # {
    #     "description": "Event Name — Tier Name — Guest Name",
    #     "unit_price_gross": "121.00",
    #     "discount_amount": "10.00",
    #     "net_amount": "91.74",
    #     "vat_amount": "19.26",
    #     "vat_rate": "21.00",
    # }
    line_items = models.JSONField(default=list, blank=True)

    # Seller snapshot (org at time of purchase — NOT editable)
    seller_name = models.CharField(max_length=255)
    seller_vat_id = models.CharField(max_length=20, blank=True, default="")
    seller_vat_country = models.CharField(max_length=2, blank=True, default="")
    seller_address = models.TextField(blank=True, default="")
    seller_email = models.EmailField(blank=True, default="")

    # Buyer snapshot (editable in DRAFT)
    buyer_name = models.CharField(max_length=255)
    buyer_vat_id = models.CharField(max_length=20, blank=True, default="")
    buyer_vat_country = models.CharField(max_length=2, blank=True, default="")
    buyer_address = models.TextField(blank=True, default="")
    buyer_email = models.EmailField(blank=True, default="")

    issued_at = models.DateTimeField(null=True, blank=True)
    pdf_file = ProtectedFileField(upload_to="invoices/attendee/", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_session_id"],
                name="unique_attendee_invoice_per_session",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["user", "created_at"]),
        ]

    @property
    def has_mixed_vat_rates(self) -> bool:
        """Whether the line items carry more than one VAT rate (a mixed-rate cart, #846).

        The stored ``vat_rate`` header column is a scalar taken from the first
        payment; a multi-tier cart can mix rates (e.g. a 22% standard tier and a
        10% reduced one), and the rendered totals label must then not claim any
        single rate — the per-rate breakdown lives in the line items.
        """
        # ponytail: only the PDF template consults this flag. AttendeeInvoiceSchema and
        # the admin still present the scalar ``vat_rate`` unguarded, so an API/admin
        # consumer can show a single rate a mixed cart never had. Upgrade path (#897):
        # expose this flag (or the per-rate line breakdown) on the schema before any
        # frontend invoice view renders ``vat_rate``.
        items: list[InvoiceLineItemDict] = self.line_items
        return len({item["vat_rate"] for item in items}) > 1

    def __str__(self) -> str:
        return f"{self.invoice_number} ({self.seller_name})"


class AttendeeInvoiceCreditNote(EmailDeliverableMixin, TimeStampedModel):
    """Credit note for refunds on already-issued attendee invoices."""

    invoice = models.ForeignKey(
        AttendeeInvoice,
        on_delete=models.PROTECT,
        related_name="credit_notes",
    )
    credit_note_number = models.CharField(max_length=50, unique=True)

    amount_gross = models.DecimalField(max_digits=10, decimal_places=2)
    amount_net = models.DecimalField(max_digits=10, decimal_places=2)
    amount_vat = models.DecimalField(max_digits=10, decimal_places=2)

    line_items = models.JSONField(default=list, blank=True)
    payments = models.ManyToManyField(
        "events.Payment",
        blank=True,
        related_name="attendee_credit_notes",
    )
    refunds = models.ManyToManyField(
        "events.Refund",
        related_name="credit_notes",
        blank=True,
        help_text="The refund attempts this credit note covers (amount-aware partial refunds).",
    )

    issued_at = models.DateTimeField(null=True, blank=True)
    pdf_file = ProtectedFileField(upload_to="invoices/attendee/credit_notes/", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.credit_note_number} (for {self.invoice.invoice_number})"
