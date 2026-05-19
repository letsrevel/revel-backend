"""seats_held is computed from pending unexpired offers."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, WaitlistOffer
from events.schema.event import EventBaseSchema

pytestmark = pytest.mark.django_db


def test_seats_held_counts_pending_unexpired(event: Event, revel_user_factory: RevelUserFactory) -> None:
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(minutes=1),  # expired
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.CLAIMED,
    )

    assert EventBaseSchema.resolve_seats_held(event) == 1


def test_seats_held_zero_when_no_offers(event: Event) -> None:
    assert EventBaseSchema.resolve_seats_held(event) == 0


def test_seats_held_ignores_other_events(
    event: Event, public_event: Event, revel_user_factory: RevelUserFactory
) -> None:
    WaitlistOffer.objects.create(
        event=public_event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    assert EventBaseSchema.resolve_seats_held(event) == 0
    assert EventBaseSchema.resolve_seats_held(public_event) == 1
