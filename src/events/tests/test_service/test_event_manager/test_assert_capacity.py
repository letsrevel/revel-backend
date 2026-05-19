"""EventManager._assert_capacity with pending offers."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from conftest import RevelUserFactory  # type: ignore[import-not-found]
from events.models import Event, EventRSVP, WaitlistOffer
from events.service.event_manager.manager import EventManager
from events.service.event_manager.types import UserIsIneligibleError

pytestmark = pytest.mark.django_db


def _set_rsvp_event(event: Event, capacity: int) -> None:
    """Configure an RSVP-only event (no tickets) with given capacity."""
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = False
    event.max_attendees = capacity
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()


def test_non_offer_holder_blocked_by_reserved_spots(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    _set_rsvp_event(event, capacity=5)
    for _ in range(4):
        EventRSVP.objects.create(event=event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)
    holder = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=holder,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    intruder = revel_user_factory()
    with pytest.raises(UserIsIneligibleError):
        EventManager(intruder, event)._assert_capacity(use_tickets=False, tier=None)


def test_offer_holder_passes_capacity(event: Event, revel_user_factory: RevelUserFactory) -> None:
    _set_rsvp_event(event, capacity=5)
    for _ in range(4):
        EventRSVP.objects.create(event=event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)
    holder = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=holder,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    # Must not raise.
    EventManager(holder, event)._assert_capacity(use_tickets=False, tier=None)


def test_expired_offer_does_not_block(event: Event, revel_user_factory: RevelUserFactory) -> None:
    _set_rsvp_event(event, capacity=5)
    for _ in range(4):
        EventRSVP.objects.create(event=event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)
    expired_holder = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=expired_holder,
        expires_at=timezone.now() - dt.timedelta(minutes=1),  # already past
        batch_id=uuid.uuid4(),
    )

    intruder = revel_user_factory()
    EventManager(intruder, event)._assert_capacity(use_tickets=False, tier=None)
