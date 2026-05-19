"""End-to-end waitlist lifecycle tests.

These tests exercise multiple components together (cancellation service ->
waitlist service -> eligibility gates -> claim hook) to verify the integrated
behavior, not just individual unit boundaries.
"""

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, EventWaitList, WaitlistOffer
from events.service import waitlist_service
from events.service.event_manager.manager import EventManager
from events.service.event_manager.types import UserIsIneligibleError

pytestmark = pytest.mark.django_db


def _configure_rsvp_event(event: Event, *, capacity: int, batch_size: int = 3) -> None:
    """Enable advanced waitlist on the event with FIFO selection."""
    event.requires_ticket = False
    event.max_attendees = capacity
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.waitlist_batch_size = batch_size
    event.save()


def _fill_with_yes_rsvps(event: Event, factory: RevelUserFactory, n: int) -> list[RevelUser]:
    """Create N YES RSVPs and set the denormalized attendee_count field."""
    users = [factory() for _ in range(n)]
    for u in users:
        EventRSVP.objects.create(event=event, user=u, status=EventRSVP.RsvpStatus.YES)
    Event.objects.filter(pk=event.pk).update(attendee_count=n)
    event.refresh_from_db()
    return users


def _add_waitlist(event: Event, factory: RevelUserFactory, n: int) -> list[RevelUser]:
    """Create N waitlist entries (FIFO determined by created_at insertion order)."""
    waiters = []
    base = timezone.now() - dt.timedelta(minutes=10)
    for i in range(n):
        u = factory()
        entry = EventWaitList.objects.create(event=event, user=u)
        # Force deterministic FIFO ordering with monotonically increasing timestamps.
        EventWaitList.objects.filter(pk=entry.pk).update(created_at=base + dt.timedelta(seconds=i))
        waiters.append(u)
    return waiters


class TestCancellationToOfferFlow:
    """RSVP YES -> NO on a full event should free exactly one seat and create one offer."""

    def test_yes_to_no_cancellation_creates_one_offer_for_fifo_first(
        self, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        # Capacity 6 with 5 live YES RSVPs allows the YES->NO transition to pass
        # `_assert_capacity` (which reads live RSVP count). The denormalized
        # `attendee_count` (used by process_waitlist) is bumped to 6 to simulate
        # a "full" event; we decrement it to 5 after cancellation to reflect
        # the freed seat. This exposes the exact "one spot freed -> one offer"
        # math from the design spec.
        _configure_rsvp_event(event, capacity=6, batch_size=3)
        attendees = _fill_with_yes_rsvps(event, revel_user_factory, n=5)
        Event.objects.filter(pk=event.pk).update(attendee_count=6)
        waitlisters = _add_waitlist(event, revel_user_factory, n=7)

        with mock.patch.object(waitlist_service, "_dispatch_offer_notifications"):
            # User-side cancellation through the EventManager.
            EventManager(attendees[0], event).rsvp(EventRSVP.RsvpStatus.NO)
            # In transactional tests, on_commit hooks do not fire, so we invoke
            # the service directly to simulate the post-commit task running.
            # Also reflect the seat being freed in the denormalized count.
            Event.objects.filter(pk=event.pk).update(attendee_count=5)
            result = waitlist_service.process_waitlist_for_event(event.id)

        assert result.status == "ok"
        # Only one spot freed: cap = min(batch_size=3, available=1) = 1 offer.
        offers = WaitlistOffer.objects.filter(event=event, status=WaitlistOffer.Status.PENDING)
        assert offers.count() == 1
        # FIFO: the first waitlister got it.
        first_offer = offers.first()
        assert first_offer is not None
        assert first_offer.user_id == waitlisters[0].id


class TestOfferHolderClaimsViaRsvp:
    """A user with a pending offer can RSVP YES; the offer flips to CLAIMED."""

    def test_claim_via_rsvp_yes(self, event: Event, revel_user_factory: RevelUserFactory) -> None:
        _configure_rsvp_event(event, capacity=1, batch_size=1)
        # 0 attendees: the offer holder represents a reservation only.
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        event.refresh_from_db()

        me = revel_user_factory()
        EventWaitList.objects.create(event=event, user=me)
        offer = WaitlistOffer.objects.create(
            event=event,
            user=me,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )

        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.YES)

        offer.refresh_from_db()
        assert offer.status == WaitlistOffer.Status.CLAIMED
        assert offer.claimed_at is not None
        assert not EventWaitList.objects.filter(event=event, user=me).exists()


class TestNonOfferHolderBlocked:
    """A non-offer-holder trying to RSVP YES while seats are reserved must be blocked."""

    def test_intruder_gets_reserved_reason(self, event: Event, revel_user_factory: RevelUserFactory) -> None:
        _configure_rsvp_event(event, capacity=5, batch_size=1)
        _fill_with_yes_rsvps(event, revel_user_factory, n=4)
        holder = revel_user_factory()
        WaitlistOffer.objects.create(
            event=event,
            user=holder,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )

        intruder = revel_user_factory()
        with pytest.raises(UserIsIneligibleError) as exc:
            EventManager(intruder, event).rsvp(EventRSVP.RsvpStatus.YES)

        eligibility = exc.value.eligibility
        assert eligibility.allowed is False


class TestOfferExpiryTriggersNextBatch:
    """expire_waitlist_offers_task flips expired offers and enqueues the next batch."""

    def test_expired_offer_flips_status_and_enqueues(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        from events.tasks import expire_waitlist_offers_task

        _configure_rsvp_event(event, capacity=5, batch_size=1)
        Event.objects.filter(pk=event.pk).update(attendee_count=4)

        u1 = revel_user_factory()
        WaitlistOffer.objects.create(
            event=event,
            user=u1,
            expires_at=timezone.now() - dt.timedelta(seconds=1),
            batch_id=uuid.uuid4(),
        )

        with mock.patch("events.tasks.process_waitlist_for_event_task.delay") as mocked:
            result = expire_waitlist_offers_task()

        assert result["expired"] == 1
        mocked.assert_called_once_with(str(event.id))

        flipped = WaitlistOffer.objects.get(user=u1, event=event)
        assert flipped.status == WaitlistOffer.Status.EXPIRED


class TestLeaveWaitlistExpiresOffer:
    """Leaving the waitlist with a pending offer expires it (full endpoint coverage)."""

    def test_leave_waitlist_with_pending_offer_expires_it(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        _configure_rsvp_event(event, capacity=5, batch_size=1)

        me = revel_user_factory()
        EventWaitList.objects.create(event=event, user=me)
        offer = WaitlistOffer.objects.create(
            event=event,
            user=me,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )

        refresh = RefreshToken.for_user(me)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
        url = reverse("api:leave_waitlist", kwargs={"event_id": event.pk})

        with mock.patch("events.controllers.event_public.attendance.enqueue_waitlist_processing") as enqueue_mock:
            response = client.delete(url)

        assert response.status_code == 200
        assert response.json()["message"] == "Successfully left the waitlist."

        offer.refresh_from_db()
        assert offer.status == WaitlistOffer.Status.EXPIRED
        assert not EventWaitList.objects.filter(event=event, user=me).exists()
        # The endpoint should have signaled the next batch processing.
        enqueue_mock.assert_called_once_with(event.id)
