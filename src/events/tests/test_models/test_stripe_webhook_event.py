"""Tests for the StripeWebhookEvent model."""

import pytest
from django.core.exceptions import ValidationError

from events.models import StripeWebhookEvent

pytestmark = pytest.mark.django_db


def test_event_id_unique() -> None:
    """A duplicate event_id is rejected (full_clean via TimeStampedModel.save)."""
    StripeWebhookEvent.objects.create(event_id="evt_1", event_type="checkout.session.completed")
    with pytest.raises(ValidationError):
        StripeWebhookEvent.objects.create(event_id="evt_1", event_type="checkout.session.completed")


def test_defaults() -> None:
    """New rows default to PROCESSING with empty account and payload."""
    row = StripeWebhookEvent.objects.create(event_id="evt_2", event_type="charge.refunded")
    assert row.outcome == StripeWebhookEvent.Outcome.PROCESSING
    assert row.account == ""
    assert row.payload == {}
    assert row.livemode is False


def test_str() -> None:
    """__str__ combines type and id."""
    row = StripeWebhookEvent.objects.create(event_id="evt_3", event_type="account.updated")
    assert str(row) == "account.updated evt_3"
