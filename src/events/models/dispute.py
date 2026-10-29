"""Card disputes (chargebacks) raised against organizer payments.

Record-only mirror of Stripe's ``charge.dispute.*`` events, kept next to the
payment data so organizers can see open disputes and their evidence deadlines.
One Stripe charge/payment intent can back several ``Payment`` rows (batch
checkout), so a dispute is linked to them by payment-intent id rather than a
single foreign key.
"""

import typing as t

from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from .payment import Payment


class Dispute(TimeStampedModel):
    """A Stripe dispute, upserted from ``charge.dispute.created|updated|closed``."""

    class DisputeStatus(models.TextChoices):
        WARNING_NEEDS_RESPONSE = "warning_needs_response", _("Warning: needs response")
        WARNING_UNDER_REVIEW = "warning_under_review", _("Warning: under review")
        WARNING_CLOSED = "warning_closed", _("Warning: closed")
        NEEDS_RESPONSE = "needs_response", _("Needs response")
        UNDER_REVIEW = "under_review", _("Under review")
        WON = "won", _("Won")
        LOST = "lost", _("Lost")
        PREVENTED = "prevented", _("Prevented")

    class DisputeReason(models.TextChoices):
        BANK_CANNOT_PROCESS = "bank_cannot_process", _("Bank cannot process")
        CHECK_RETURNED = "check_returned", _("Check returned")
        CREDIT_NOT_PROCESSED = "credit_not_processed", _("Credit not processed")
        CUSTOMER_INITIATED = "customer_initiated", _("Customer initiated")
        DEBIT_NOT_AUTHORIZED = "debit_not_authorized", _("Debit not authorized")
        DUPLICATE = "duplicate", _("Duplicate")
        FRAUDULENT = "fraudulent", _("Fraudulent")
        GENERAL = "general", _("General")
        INCORRECT_ACCOUNT_DETAILS = "incorrect_account_details", _("Incorrect account details")
        INSUFFICIENT_FUNDS = "insufficient_funds", _("Insufficient funds")
        PRODUCT_NOT_RECEIVED = "product_not_received", _("Product not received")
        PRODUCT_UNACCEPTABLE = "product_unacceptable", _("Product unacceptable")
        SUBSCRIPTION_CANCELED = "subscription_canceled", _("Subscription canceled")
        UNRECOGNIZED = "unrecognized", _("Unrecognized")

    # Final: Stripe sends no further changes after these. ``warning_closed`` is not final: an
    # inquiry that was closed can still escalate to a full dispute.
    FINAL_STATUSES: t.ClassVar[frozenset[str]] = frozenset(
        {DisputeStatus.WON, DisputeStatus.LOST, DisputeStatus.PREVENTED}
    )
    # Closed for the organizer: nothing left to answer (final statuses plus a closed inquiry).
    CLOSED_STATUSES: t.ClassVar[frozenset[str]] = FINAL_STATUSES | {DisputeStatus.WARNING_CLOSED}
    # Inquiry phase: the customer's bank is asking, no money has moved.
    WARNING_STATUSES: t.ClassVar[frozenset[str]] = frozenset(
        {DisputeStatus.WARNING_NEEDS_RESPONSE, DisputeStatus.WARNING_UNDER_REVIEW, DisputeStatus.WARNING_CLOSED}
    )
    # Full dispute awaiting or under review.
    ACTIVE_STATUSES: t.ClassVar[frozenset[str]] = frozenset({DisputeStatus.NEEDS_RESPONSE, DisputeStatus.UNDER_REVIEW})

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
    # No ``choices``: Stripe may add statuses; an unknown one must be stored, not crash the webhook.
    status = models.CharField(max_length=50, db_index=True)
    evidence_due_by = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Dispute {self.stripe_dispute_id} ({self.status}) of {self.amount} {self.currency}"

    @property
    def status_label(self) -> str:
        """Translated status label, falling back to the raw value for statuses Revel does not know."""
        try:
            return str(self.DisputeStatus(self.status).label)
        except ValueError:
            return self.status

    @property
    def reason_label(self) -> str:
        """Translated reason label, falling back to the raw code with underscores replaced."""
        try:
            return str(self.DisputeReason(self.reason).label)
        except ValueError:
            return self.reason.replace("_", " ")

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
