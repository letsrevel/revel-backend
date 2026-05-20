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


def test_seats_held_does_not_count_pending_but_expired(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    """A PENDING-but-time-expired offer must not count as held."""
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(minutes=1),  # past
        batch_id=uuid.uuid4(),
    )
    assert EventBaseSchema.resolve_seats_held(event) == 0


def test_seats_held_does_not_count_expired_status_in_future(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    """An EXPIRED-status offer with future expires_at must not count as held."""
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),  # future
        batch_id=uuid.uuid4(),
    )
    offer.status = WaitlistOffer.Status.EXPIRED
    offer.save(update_fields=["status"])
    assert EventBaseSchema.resolve_seats_held(event) == 0


def test_seats_held_does_not_count_cutoff_batch_offers(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    """Cutoff-batch offers do not reserve seats (Wave 1 F3 semantics)."""
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        is_cutoff_batch=True,
    )
    assert EventBaseSchema.resolve_seats_held(event) == 0
