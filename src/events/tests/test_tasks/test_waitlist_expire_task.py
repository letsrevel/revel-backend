"""Tests for expire_waitlist_offers_task."""

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, WaitlistOffer
from events.tasks import expire_waitlist_offers_task

pytestmark = pytest.mark.django_db


def test_flips_expired_pending_offers(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Expired PENDING offers flip to EXPIRED; fresh ones are untouched."""
    u1, u2 = revel_user_factory(), revel_user_factory()
    old = WaitlistOffer.objects.create(
        event=event,
        user=u1,
        expires_at=timezone.now() - dt.timedelta(minutes=1),
        batch_id=uuid.uuid4(),
    )
    fresh = WaitlistOffer.objects.create(
        event=event,
        user=u2,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    result = expire_waitlist_offers_task()
    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.status == WaitlistOffer.WaitlistOfferStatus.EXPIRED
    assert fresh.status == WaitlistOffer.WaitlistOfferStatus.PENDING
    assert result["expired"] == 1


def test_enqueues_next_batch_per_event(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Each event with expiring offers triggers exactly one process call."""
    u = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=u,
        expires_at=timezone.now() - dt.timedelta(minutes=1),
        batch_id=uuid.uuid4(),
    )
    with mock.patch("events.tasks.process_waitlist_for_event_task.delay") as mocked:
        expire_waitlist_offers_task()
    assert mocked.call_count == 1
    mocked.assert_called_with(str(event.id))
