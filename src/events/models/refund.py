"""Refund attempts against ticket Payments.

One row per refund attempt (full or partial). ``Payment.refund_amount`` is a
denormalized running total of SUCCEEDED rows, maintained by the
``charge.refunded`` webhook. Rows are the anchor the webhook matcher uses to
attribute inbound Stripe refunds exactly.
"""

from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class Refund(TimeStampedModel):
    """A single refund attempt against a ticket Payment."""

    class RefundStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Source(models.TextChoices):
        ORGANIZER_API = "organizer_api", "Organizer via API"
        USER_CANCELLATION = "user_cancellation", "User self-service cancellation"
        EVENT_CANCELLATION = "event_cancellation", "Bulk event cancellation"
        STRIPE_DASHBOARD = "stripe_dashboard", "Stripe dashboard"

    # CASCADE mirrors Payment→Ticket→User (GDPR cascade; Stripe keeps the financial record).
    payment = models.ForeignKey("events.Payment", on_delete=models.CASCADE, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=RefundStatus.choices, default=RefundStatus.PENDING, db_index=True)
    stripe_refund_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    failure_reason = models.TextField(blank=True, default="")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_refunds",
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    source = models.CharField(max_length=20, choices=Source.choices, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Refund {self.id} ({self.status}) of {self.amount} {self.currency} on Payment {self.payment_id}"
