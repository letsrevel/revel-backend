"""Tests for the post_delete signal that revokes pending offers when an entry is removed."""

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, EventWaitList, WaitlistOffer

pytestmark = pytest.mark.django_db


def test_delete_entry_revokes_pending_offer_and_enqueues_processing(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.WaitlistOfferStatus.PENDING,
    )

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        entry.delete()

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.WaitlistOfferStatus.REVOKED
    enqueue_mock.assert_called_once_with(event.id)


def test_delete_entry_does_not_touch_claimed_offer(event: Event, revel_user_factory: RevelUserFactory) -> None:
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.WaitlistOfferStatus.CLAIMED,
        claimed_at=timezone.now(),
    )

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        entry.delete()

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.WaitlistOfferStatus.CLAIMED
    enqueue_mock.assert_not_called()


def test_delete_entry_with_no_offer_is_noop(event: Event, revel_user_factory: RevelUserFactory) -> None:
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        entry.delete()

    assert not WaitlistOffer.objects.filter(event=event, user=user).exists()
    enqueue_mock.assert_not_called()
