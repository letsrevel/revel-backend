"""Celery tasks for Stripe webhook maintenance.

The task carries an explicit registered name (``events.prune_stripe_webhook_events``),
so the Celery-beat schedule defined in migration 0079 — which references the task by
name string — is unaffected.
"""

from datetime import timedelta

import structlog
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from events.models import StripeWebhookEvent

logger = structlog.get_logger(__name__)


@shared_task(name="events.prune_stripe_webhook_events")
def prune_stripe_webhook_events() -> int:
    """Delete StripeWebhookEvent rows past the retention window.

    Stripe retries deliveries for at most 3 days, so pruned event ids can
    never be legitimately redelivered — the idempotency guarantee survives
    pruning. Single bulk DELETE; no per-row commits.
    """
    cutoff = timezone.now() - timedelta(days=settings.STRIPE_WEBHOOK_EVENT_RETENTION_DAYS)
    deleted, _ = StripeWebhookEvent.objects.filter(created_at__lt=cutoff).delete()
    logger.info("stripe_webhook_events_pruned", deleted=deleted)
    return deleted
