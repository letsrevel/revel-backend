from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel

from .ticket import _get_payment_default_expiry


class Payment(TimeStampedModel):
    class PaymentStatus(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"

    class RefundStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    # note: we cascade on ticket and user deletion because stripe holds financial records for us/the org
    # this is not THE BEST solution, but it's the simplest to keep local GDPR compliance.
    # In the future, a more complex solution will be proposed
    ticket = models.OneToOneField("events.Ticket", on_delete=models.CASCADE, related_name="payment")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    # Not unique: batch purchases share the same session_id across multiple tickets
    stripe_session_id = models.CharField(max_length=255, db_index=True)
    # Groups the Payment rows created together in one checkout "reserve" step.
    # Set at reserve time (before the Stripe session exists) and used as the
    # sibling-grouping key by the interactive cancel/resume/cleanup paths, since
    # stripe_session_id is "" until the session endpoint stamps it. See #632.
    # Nullable: legacy rows are backfilled (migration 0095); every new online
    # checkout Payment sets it in code.
    reservation_id = models.UUIDField(null=True, blank=True, db_index=True, editable=False)
    stripe_payment_intent_id = models.CharField(
        max_length=255, null=True, blank=True, db_index=True, help_text="Stripe PaymentIntent ID for refund processing"
    )
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)

    # Ticket sale VAT breakdown (calculated in-house, all nullable for historical payments)
    net_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="Ticket price excluding VAT."
    )
    vat_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="VAT portion of the ticket price."
    )
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, help_text="VAT rate snapshot at purchase time."
    )

    # Platform fee VAT breakdown (for Revel's accounting, all nullable for historical payments)
    platform_fee_net = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="Platform fee excluding VAT."
    )
    platform_fee_vat = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="VAT portion of the platform fee."
    )
    platform_fee_vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, help_text="Platform fee VAT rate snapshot."
    )
    platform_fee_reverse_charge = models.BooleanField(
        default=False, help_text="Whether reverse charge applies to the platform fee (EU B2B cross-border)."
    )

    # Buyer billing info snapshot for attendee invoicing
    buyer_billing_snapshot = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Buyer billing info snapshot at checkout time for attendee invoice generation.",
    )

    refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual amount refunded. May be less than amount when a partial refund policy applies.",
    )
    refund_status = models.CharField(
        max_length=20,
        choices=RefundStatus.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    stripe_refund_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe refund object id. Used to match webhook refund events to this Payment.",
    )
    refund_failure_reason = models.TextField(blank=True, default="")
    refunded_at = models.DateTimeField(null=True, blank=True)

    raw_response = models.JSONField(blank=True, default=dict)  # To store the full webhook event for auditing
    expires_at = models.DateTimeField(default=_get_payment_default_expiry, db_index=True, editable=False)
    # Incident hold (#756): stamped by events.hold_mismatch_payments when this row is
    # implicated in a recorded stripe_session_total_mismatch — the PENDING rows ARE the
    # incident evidence. Non-null exempts the row from cleanup_expired_payments until
    # the retention window (INCIDENT_HOLD_RETENTION in events/tasks/payments.py) lapses;
    # an operator resolves earlier by clearing the field in the Payment admin.
    incident_hold_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Evidence hold for a recorded money-correctness incident. While set, the payment "
            "expiry sweep retains this row; clear it once the incident is resolved to release "
            "the row back to the normal cleanup schedule."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_session_id", "ticket"],
                name="unique_payment_per_session_ticket",
            ),
        ]
        indexes = [
            # Partial index: near-empty in practice (holds are one-occurrence incidents),
            # so the sweep's retention-lapse branch (incident_hold_at < cutoff, which
            # implies NOT NULL) stays an index scan without taxing every Payment write.
            models.Index(
                fields=["incident_hold_at"],
                name="payment_incident_hold_idx",
                condition=models.Q(incident_hold_at__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.id} for Ticket {self.ticket.id}"

    def has_expired(self) -> bool:
        """Return whether a payment has expired."""
        return self.expires_at < timezone.now()

    @staticmethod
    def stripe_mode() -> str:
        """Stripe mode."""
        key: str = settings.STRIPE_SECRET_KEY
        return "test" if key.startswith("sk_test_") else "live"

    def stripe_dashboard_url(self) -> str:
        """Return the stripe dashboard URL."""
        mode: str = self.stripe_mode()
        if self.stripe_payment_intent_id:
            return f"https://dashboard.stripe.com/{mode}/payments/{self.stripe_payment_intent_id}"
        return f"https://dashboard.stripe.com/{mode}/checkout/sessions/{self.stripe_session_id}"
