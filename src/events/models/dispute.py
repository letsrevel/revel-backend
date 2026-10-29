"""Card disputes (chargebacks) raised against organizer payments.

Record-only mirror of Stripe's ``charge.dispute.*`` events, kept next to the
payment data so organizers can see open disputes and their evidence deadlines.
One Stripe charge/payment intent can back several ``Payment`` rows (batch
checkout), so a dispute is linked to them by payment-intent id rather than a
single foreign key.
"""

import typing as t

from django.db import models

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from .payment import Payment


class Dispute(TimeStampedModel):
    """A Stripe dispute, upserted from ``charge.dispute.created|updated|closed``."""

    class DisputeStatus(models.TextChoices):
        WARNING_NEEDS_RESPONSE = "warning_needs_response", "Warning: needs response"
        WARNING_UNDER_REVIEW = "warning_under_review", "Warning: under review"
        WARNING_CLOSED = "warning_closed", "Warning: closed"
        NEEDS_RESPONSE = "needs_response", "Needs response"
        UNDER_REVIEW = "under_review", "Under review"
        WON = "won", "Won"
        LOST = "lost", "Lost"
        PREVENTED = "prevented", "Prevented"

    # Statuses after which Stripe sends no further changes to the dispute.
    CLOSED_STATUSES: t.ClassVar[frozenset[str]] = frozenset(
        {DisputeStatus.WON, DisputeStatus.LOST, DisputeStatus.WARNING_CLOSED, DisputeStatus.PREVENTED}
    )

    # Null when neither the connected account nor a matching Payment identifies the organization.
    organization = models.ForeignKey(
        "events.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="disputes"
    )
    stripe_dispute_id = models.CharField(max_length=255, unique=True)
    stripe_charge_id = models.CharField(max_length=255, blank=True, default="")
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    reason = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=30, choices=DisputeStatus.choices, db_index=True)
    evidence_due_by = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Dispute {self.stripe_dispute_id} ({self.status}) of {self.amount} {self.currency}"

    @property
    def is_closed(self) -> bool:
        """Whether the dispute reached a terminal status."""
        return self.status in self.CLOSED_STATUSES

    def linked_payments(self) -> "models.QuerySet[Payment]":
        """Return the ticket Payment rows that share this dispute's payment intent."""
        from .payment import Payment

        if not self.stripe_payment_intent_id:
            return Payment.objects.none()
        return Payment.objects.filter(stripe_payment_intent_id=self.stripe_payment_intent_id)
