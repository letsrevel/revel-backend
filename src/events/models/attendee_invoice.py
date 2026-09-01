"""Attendee invoice and credit note models.

Invoices issued to attendees (buyers) on behalf of organizers (sellers)
for online ticket purchases. The organizer is the legal seller; Revel acts
as an intermediary generating and delivering invoices.
"""

import typing as t
from decimal import Decimal, InvalidOperation

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


class InvoiceVatBucketDict(t.TypedDict):
    """One VAT-rate bucket derived from an invoice's line items (#897).

    Amounts are summed independently from the stored per-item values rather
    than derived from one another, and they reconcile exactly to the invoice's
    ``total_net`` / ``total_vat`` / ``total_gross`` header columns for every
    invoice written through the application: ``generate_attendee_invoice``
    builds both sides from one set of ``Payment`` rows, and the only edit that
    can change them afterwards -- rewriting a DRAFT's ``line_items`` --
    recomputes the header from the new lines rather than letting a caller
    state it (#911).

    That is an application-layer invariant, not a database constraint. These
    are plain ``DecimalField``s, so a raw ``QuerySet.update()`` or a shell
    write can still desync them, and no read path re-checks: a DRAFT desynced
    that way serves a header and a breakdown that disagree in one response.
    Issuance is guarded (``_assert_totals_reconcile``), so such a row cannot
    become a legal document -- but do not treat a DRAFT's two figures as
    reconciled by construction when the row's provenance is unknown.

    The scalar ``vat_rate`` header column is the exception and always was: it
    names the first line's rate only. Read ``has_mixed_vat_rates`` before
    rendering it, and these buckets instead when it is true.
    """

    vat_rate: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal


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

    While in DRAFT, the buyer snapshot, currency, reverse-charge flag, discount
    label and line items are editable; the seller (org) snapshot and the money
    columns derived from the line items are not (see ``EDITABLE_DRAFT_FIELDS`` /
    ``DERIVED_TOTAL_FIELDS`` in ``attendee_invoice_service``). Once ISSUED, the
    invoice is immutable (can only be cancelled via credit note).
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

    # Totals. Derived, not settable on their own through the API: computed from the
    # Payment rows at generation and recomputed from ``line_items`` whenever a
    # DRAFT's lines are edited, so no edit can drift them from ``vat_breakdown``
    # (#911). A raw ``.update()`` still can -- see ``InvoiceVatBucketDict``.
    total_gross = models.DecimalField(max_digits=10, decimal_places=2)
    total_net = models.DecimalField(max_digits=10, decimal_places=2)
    total_vat = models.DecimalField(max_digits=10, decimal_places=2)
    # A snapshot of the FIRST payment's rate, not an invoice-wide fact: a
    # multi-tier cart (#846) mixes rates and this column names only one of them.
    # Consult ``has_mixed_vat_rates`` before rendering it; ``vat_breakdown`` has
    # the per-rate truth (#897).
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    currency = models.CharField(max_length=3)
    reverse_charge = models.BooleanField(default=False)

    # Discount
    discount_code_text = models.CharField(max_length=64, blank=True, default="")
    discount_amount_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Line items (JSON snapshot, editable in DRAFT -- and the source the header
    # totals above are recomputed from on every such edit)
    # Structure per item:
    # {
    #     "description": "Event Name — Tier Name — Guest Name",
    #     "unit_price_gross": "111.00",
    #     "discount_amount": "10.00",
    #     "net_amount": "91.74",
    #     "vat_amount": "19.26",
    #     "vat_rate": "21.00",
    # }
    # unit_price_gross is the amount actually charged, post-discount (net + vat);
    # discount_amount is informational only.
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
    def vat_breakdown(self) -> list[InvoiceVatBucketDict]:
        """Line items grouped by VAT rate, ascending by rate.

        The stored ``vat_rate`` header column is a scalar taken from the first
        payment, so a multi-tier cart (#846) can mix rates and the header cannot
        describe the document. This breakdown can: it is what an API consumer,
        the admin, and any rendered totals block should read instead.

        Derived on every access rather than stored: ``UpdateAttendeeInvoiceSchema``
        lets an organizer rewrite ``line_items`` on a DRAFT invoice, and a stored
        breakdown would go stale on exactly those rows.
        """
        items: list[InvoiceLineItemDict] = self.line_items
        buckets: dict[Decimal, InvoiceVatBucketDict] = {}
        for item in items:
            # Group on the parsed Decimal, never the raw string: "22.0" and
            # "22.00" are one rate, and comparing strings reported such a cart
            # as mixed. Pad to 2dp (the column's decimal_places) so the bucket
            # key -- and thus the serialized rate -- doesn't depend on which
            # exponent happened to be seen first, but NEVER round: a rate with
            # genuine sub-cent precision (reachable only through a hand-edited
            # draft, since InvoiceLineItemSchema does not bound the scale) keeps
            # its own bucket. Merging 22.003 with 22.004 would label the bucket
            # 22.00 -- a rate neither line carries -- and report a genuinely
            # mixed invoice as single-rate, the exact falsification this
            # property exists to prevent.
            raw_rate = Decimal(item["vat_rate"])
            try:
                padded_rate = raw_rate.quantize(Decimal("0.01"))
            except InvalidOperation:
                # A magnitude too large to express at 2dp in the default context
                # (e.g. "1E+30"). Absurd, and only a hand-edited draft can store
                # it -- but this property is on EVERY read path for the invoice
                # (schema, admin, PDF), so raising here would leave the row
                # permanently unreadable AND unrepairable: the PATCH that would
                # fix it renders the same schema. Bucket it as-is instead.
                padded_rate = raw_rate
            rate = padded_rate if padded_rate == raw_rate else raw_rate
            bucket = buckets.setdefault(
                rate,
                InvoiceVatBucketDict(
                    vat_rate=rate,
                    net_amount=Decimal("0.00"),
                    vat_amount=Decimal("0.00"),
                    gross_amount=Decimal("0.00"),
                ),
            )
            bucket["net_amount"] += Decimal(item["net_amount"])
            bucket["vat_amount"] += Decimal(item["vat_amount"])
            bucket["gross_amount"] += Decimal(item["unit_price_gross"])
        return [buckets[rate] for rate in sorted(buckets)]

    @property
    def has_mixed_vat_rates(self) -> bool:
        """Whether the line items carry more than one VAT rate (a mixed-rate cart, #846).

        Derived from :attr:`vat_breakdown` so the flag and the buckets cannot
        contradict each other. Consumers that render a rate must consult this
        first: on a mixed invoice no single rate applies to the totals, and the
        per-rate detail is in the breakdown.
        """
        return len(self.vat_breakdown) > 1

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
