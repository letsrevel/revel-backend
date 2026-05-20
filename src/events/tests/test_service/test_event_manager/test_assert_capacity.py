"""EventManager._assert_capacity with pending offers."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
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


def test_non_offer_holder_blocked_by_reserved_spots(event: Event, revel_user_factory: RevelUserFactory) -> None:
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


def test_cutoff_offer_does_not_block_non_holder(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Cutoff-batch offers race FCFS against real seats and must NOT reserve
    capacity. A non-offer-holder should still be able to grab a seat even if
    there are cutoff offers outstanding for the remaining spots."""
    _set_rsvp_event(event, capacity=5)
    for _ in range(4):
        EventRSVP.objects.create(event=event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)
    # 1 spot left, but many cutoff offers issued - they shouldn't count as reserving.
    for _ in range(5):
        WaitlistOffer.objects.create(
            event=event,
            user=revel_user_factory(),
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
            is_cutoff_batch=True,
        )

    intruder = revel_user_factory()
    # Must not raise - the intruder competes for the 1 real seat against cutoff holders.
    EventManager(intruder, event)._assert_capacity(use_tickets=False, tier=None)


def test_cutoff_offer_holders_not_blocked_by_each_other(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    """Multiple cutoff offer holders must all be able to attempt claim
    (they race FCFS for remaining capacity)."""
    _set_rsvp_event(event, capacity=5)
    for _ in range(3):
        EventRSVP.objects.create(event=event, user=revel_user_factory(), status=EventRSVP.RsvpStatus.YES)
    # 2 spots left, 5 cutoff offer holders. Each one's _assert_capacity should
    # pass (cutoff offers don't reserve, so count(3) + pending(0) < cap(5)).
    holders = [revel_user_factory() for _ in range(5)]
    for h in holders:
        WaitlistOffer.objects.create(
            event=event,
            user=h,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
            is_cutoff_batch=True,
        )

    for h in holders:
        EventManager(h, event)._assert_capacity(use_tickets=False, tier=None)


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
