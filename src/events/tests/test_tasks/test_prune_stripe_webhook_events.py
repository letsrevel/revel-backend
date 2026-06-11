"""Tests for the StripeWebhookEvent pruning task."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from events.models import StripeWebhookEvent
from events.tasks_stripe import prune_stripe_webhook_events

pytestmark = pytest.mark.django_db


def test_prunes_only_rows_past_retention(settings: t.Any) -> None:
    """Rows older than the retention window are deleted; newer rows survive."""
    settings.STRIPE_WEBHOOK_EVENT_RETENTION_DAYS = 90
    old = StripeWebhookEvent.objects.create(event_id="evt_old", event_type="x")
    # created_at is auto_now_add; move it back explicitly.
    StripeWebhookEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
    StripeWebhookEvent.objects.create(event_id="evt_new", event_type="x")

    deleted = prune_stripe_webhook_events()

    assert deleted == 1
    assert StripeWebhookEvent.objects.filter(event_id="evt_new").exists()
    assert not StripeWebhookEvent.objects.filter(event_id="evt_old").exists()
