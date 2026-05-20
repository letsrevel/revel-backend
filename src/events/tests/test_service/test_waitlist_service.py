"""Tests for the waitlist selection algorithm."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, EventWaitList, WaitlistOffer
from events.service import waitlist_service

pytestmark = pytest.mark.django_db


def _enable_waitlist(
    event: Event,
    *,
    batch_size: int = 0,
    lottery: bool = False,
    cutoff_date: dt.datetime | None = None,
    time_window: dt.timedelta = dt.timedelta(hours=24),
    max_attendees: int = 10,
) -> None:
    # Push event into the future so cutoff_date validation has room.
    event.start = timezone.now() + dt.timedelta(days=1)
    event.end = event.start + dt.timedelta(hours=2)
    event.waitlist_open = True
    event.waitlist_time_window = time_window
    event.waitlist_batch_size = batch_size
    event.waitlist_lottery_mode = lottery
    event.waitlist_cutoff_date = cutoff_date
    event.max_attendees = max_attendees
    event.save()


def _put_on_waitlist(event: Event, users: list[RevelUser]) -> None:
    for u in users:
        EventWaitList.objects.create(event=event, user=u)


class TestDisabledFeature:
    def test_returns_disabled_when_time_window_null(self, event: Event) -> None:
        event.waitlist_open = True
        event.waitlist_time_window = None
        event.save()
        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "disabled"

    def test_returns_disabled_when_waitlist_closed(self, event: Event) -> None:
        event.waitlist_open = False
        event.waitlist_time_window = dt.timedelta(hours=24)
        event.save()
        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "disabled"


class TestNoSpots:
    def test_returns_no_spots_when_capacity_full(self, event: Event, user: RevelUser) -> None:
        _enable_waitlist(event, max_attendees=1)
        EventRSVP.objects.create(event=event, user=user, status=EventRSVP.RsvpStatus.YES)
        Event.objects.filter(pk=event.pk).update(attendee_count=1)

        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "no_spots"


class TestOpenModeBatchSizeZero:
    def test_notifies_all_eligible_up_to_capacity(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        _enable_waitlist(event, batch_size=0, max_attendees=10)
        Event.objects.filter(pk=event.pk).update(attendee_count=8)
        users = [revel_user_factory() for _ in range(5)]
        _put_on_waitlist(event, users)

        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.offers_created == 2  # 10 - 8 = 2 free spots
        offers = WaitlistOffer.objects.filter(event=event)
        assert all(o.is_cutoff_batch is False for o in offers)


class TestBatchedFIFO:
    def test_picks_first_N_by_created_at(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        _enable_waitlist(event, batch_size=2, max_attendees=10)
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        u1, u2, u3 = (revel_user_factory() for _ in range(3))
        _put_on_waitlist(event, [u1, u2, u3])

        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.offers_created == 2
        offered = set(WaitlistOffer.objects.values_list("user_id", flat=True))
        assert offered == {u1.id, u2.id}
        offers = WaitlistOffer.objects.filter(event=event)
        assert all(o.is_cutoff_batch is False for o in offers)


class TestBatchedLottery:
    def test_random_sample_from_waitlist(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_waitlist(event, batch_size=2, lottery=True, max_attendees=10)
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        users = [revel_user_factory() for _ in range(5)]
        _put_on_waitlist(event, users)

        # Deterministic sampling for assertion
        monkeypatch.setattr(
            "events.service.waitlist_service.random.sample",
            lambda population, k: list(population)[:k],
        )
        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.offers_created == 2
        offers = WaitlistOffer.objects.filter(event=event)
        assert all(o.is_cutoff_batch is False for o in offers)


class TestCutoff:
    def test_cutoff_branch_offers_to_everyone(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        past = timezone.now() - dt.timedelta(minutes=5)
        _enable_waitlist(
            event,
            batch_size=2,
            cutoff_date=past,
            max_attendees=10,
        )
        Event.objects.filter(pk=event.pk).update(attendee_count=8)
        users = [revel_user_factory() for _ in range(5)]
        _put_on_waitlist(event, users)

        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.offers_created == 5
        offers = WaitlistOffer.objects.filter(event=event)
        assert all(o.is_cutoff_batch for o in offers)

    def test_second_cutoff_call_no_ops(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        past = timezone.now() - dt.timedelta(minutes=5)
        _enable_waitlist(event, batch_size=2, cutoff_date=past, max_attendees=10)
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        users = [revel_user_factory() for _ in range(3)]
        _put_on_waitlist(event, users)

        waitlist_service.process_waitlist_for_event(event.id)
        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "cutoff_already_processed"


class TestCutoffOffersDoNotReserveCapacity:
    """Cutoff-batch offers compete FCFS against any remaining seats.

    They must NOT count toward capacity-reserving pending offer counts in
    `process_waitlist_for_event`, EventManager._assert_capacity, or the
    EligibilityService annotation. Otherwise, with N cutoff offers > S available
    seats, every offer holder is blocked from claiming.
    """

    def test_cutoff_offers_not_counted_in_no_spots_check(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """After a cutoff batch has been issued, a re-process should not see
        the cutoff offers as 'reserving' seats. The early-return guard
        `cutoff_already_processed` kicks in first, but the underlying
        `pending_count` calculation must also exclude cutoff offers."""
        past = timezone.now() - dt.timedelta(minutes=5)
        _enable_waitlist(
            event,
            batch_size=2,
            cutoff_date=past,
            max_attendees=2,
        )
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        users = [revel_user_factory() for _ in range(5)]
        _put_on_waitlist(event, users)

        # Sanity: pending_count (cutoff-batch=False filter) is 0 BEFORE the run.
        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.is_cutoff_batch is True
        assert result.offers_created == 5

        # All 5 are cutoff offers - they must not block the underlying
        # pending_count from being 0 (it just falls into "no_spots" or the
        # cutoff_already_processed branch on a re-process — neither triggers
        # the 'reserved' branch).
        pending_non_cutoff = WaitlistOffer.objects.filter(
            event=event,
            status=WaitlistOffer.WaitlistOfferStatus.PENDING,
            expires_at__gt=timezone.now(),
            is_cutoff_batch=False,
        ).count()
        assert pending_non_cutoff == 0


class TestExcludesUsersWithPendingOffers:
    def test_user_with_pending_offer_not_re_offered(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        _enable_waitlist(event, batch_size=5, max_attendees=10)
        Event.objects.filter(pk=event.pk).update(attendee_count=0)
        u1, u2 = revel_user_factory(), revel_user_factory()
        _put_on_waitlist(event, [u1, u2])
        WaitlistOffer.objects.create(
            event=event,
            user=u1,
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )

        result = waitlist_service.process_waitlist_for_event(event.id)
        assert result.status == "ok"
        assert result.offers_created == 1
        assert WaitlistOffer.objects.filter(user=u2, status="pending").exists()
