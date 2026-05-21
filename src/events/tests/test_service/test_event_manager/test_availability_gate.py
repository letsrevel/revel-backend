"""AvailabilityGate behavior with pending waitlist offers."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventWaitList, Ticket, TicketTier, WaitlistOffer
from events.service.event_manager.enums import NextStep
from events.service.event_manager.service import EligibilityService

pytestmark = pytest.mark.django_db


def _setup_full_event_with_offers(
    event: Event,
    *,
    revel_user_factory: RevelUserFactory,
    attendees: int,
    pending_offers_users: list[RevelUser],
    capacity: int = 30,
) -> None:
    """Configure the event with capacity, attendees (as tickets) and pending offers."""
    event.end = event.start + dt.timedelta(hours=2)
    event.max_attendees = capacity
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()

    tier = TicketTier.objects.create(event=event, name="General")
    for _i in range(attendees):
        attendee = revel_user_factory()
        Ticket.objects.create(guest_name="Attendee", event=event, user=attendee, tier=tier)

    for u in pending_offers_users:
        WaitlistOffer.objects.create(
            event=event,
            user=u,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )


def test_non_waitlisted_user_sees_reserved_reason(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """User not on waitlist sees the 'spots reserved' reason when capacity is held by pending offers."""
    holders = [revel_user_factory() for _ in range(5)]
    _setup_full_event_with_offers(
        event, revel_user_factory=revel_user_factory, attendees=25, pending_offers_users=holders
    )
    viewer = revel_user_factory()

    elig = EligibilityService(viewer, event).check_eligibility()
    assert elig.allowed is False
    assert elig.reason is not None
    assert "reserved" in elig.reason.lower()
    assert elig.next_step == NextStep.JOIN_WAITLIST
    assert elig.pending_offers_count == 5
    assert elig.next_batch_at is not None


def test_waitlisted_user_without_offer_sees_waiting_reason(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A user already on the waitlist (no offer) sees the 'waiting' reason and a position."""
    holders = [revel_user_factory() for _ in range(5)]
    _setup_full_event_with_offers(
        event, revel_user_factory=revel_user_factory, attendees=25, pending_offers_users=holders
    )
    viewer = revel_user_factory()
    EventWaitList.objects.create(event=event, user=viewer)

    elig = EligibilityService(viewer, event).check_eligibility()
    assert elig.allowed is False
    assert elig.next_step == NextStep.WAIT_FOR_OPEN_SPOT
    assert elig.waitlist_position == 1


def test_user_with_active_offer_passes_capacity(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A user holding an active offer passes capacity even when other offers are pending."""
    holders = [revel_user_factory() for _ in range(4)]
    _setup_full_event_with_offers(
        event, revel_user_factory=revel_user_factory, attendees=25, pending_offers_users=holders
    )
    me = revel_user_factory()
    own_offer = WaitlistOffer.objects.create(
        event=event,
        user=me,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    elig = EligibilityService(me, event).check_eligibility()
    assert elig.allowed is True
    assert elig.active_offer_expires_at == own_offer.expires_at


def test_truly_full_no_offers_returns_event_is_full(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """When the event is full on attendees alone (no pending offers), the reason is EVENT_IS_FULL."""
    _setup_full_event_with_offers(event, revel_user_factory=revel_user_factory, attendees=30, pending_offers_users=[])
    viewer = revel_user_factory()

    elig = EligibilityService(viewer, event).check_eligibility()
    assert elig.allowed is False
    assert elig.reason is not None
    assert "reserved" not in elig.reason.lower()
    assert "full" in elig.reason.lower()
