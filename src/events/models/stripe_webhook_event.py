"""Inbound Stripe webhook event log + idempotency token."""

from django.db import models

from common.models import TimeStampedModel


class StripeWebhookEvent(TimeStampedModel):
    """Log of every verified inbound Stripe webhook event.

    The unique ``event_id`` doubles as the idempotency token: ``handle_event``
    inserts the row *before* dispatching, so Stripe redeliveries trip the
    unique constraint and no-op. If a handler raises, the surrounding request
    transaction rolls this row back too, so the Stripe retry reprocesses the
    event instead of being swallowed by the dedup gate.

    Rows are pruned after ``settings.STRIPE_WEBHOOK_EVENT_RETENTION_DAYS``
    (Stripe retries deliveries for at most 3 days, so pruned ids can never be
    legitimately redelivered).
    """

    class Outcome(models.TextChoices):
        PROCESSING = "processing", "Processing"
        HANDLED = "handled", "Handled"
        UNHANDLED = "unhandled", "Unhandled"

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100, db_index=True)
    account = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe Connect account id (event.account); empty for platform-endpoint events.",
    )
    livemode = models.BooleanField(default=False)
    payload = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.PROCESSING, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} {self.event_id}"
