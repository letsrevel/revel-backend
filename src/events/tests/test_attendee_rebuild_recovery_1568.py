"""A lost attendee rebuild after a bulk ticket create is recovered (#1568).

``series_pass_service.rebuild_attendee_visibility`` is the only rebuild after a ``bulk_create`` (it skips
``post_save``). Its dispatch is now independent (``on_commit_independent``), so a broker failure does not
fail the caller, and the 15-minute sweep ``events.reconcile_attendee_counts`` dispatches the rebuild again
for an event whose stored ``attendee_count`` differs from its live count.
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.service import series_pass_service
from events.tasks import reconcile_attendee_counts

pytestmark = pytest.mark.django_db

DELAY = "events.tasks.attendees.build_attendee_visibility_flags.delay"


@pytest.fixture
def tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(event=event, name="Free", price=0, payment_method=TicketTier.PaymentMethod.FREE)


def _bulk_ticket(event: Event, tier: TicketTier, user: RevelUser) -> Ticket:
    """A ticket written as the series pass paths write it: bulk_create, so no post_save rebuild.

    Dated 10 minutes ago, past the sweep's grace period for a rebuild that is only queued.
    """
    (ticket,) = Ticket.objects.bulk_create(
        [Ticket(event=event, tier=tier, user=user, guest_name="Ada", status=Ticket.TicketStatus.ACTIVE)]
    )
    Ticket.objects.filter(pk=ticket.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
    return ticket


class TestRobustDispatch:
    def test_a_failed_enqueue_does_not_fail_the_caller(
        self, event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch(
            "events.service.series_pass_service.build_attendee_visibility_flags.delay",
            side_effect=ConnectionError("broker down"),
        ) as delay:
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.rebuild_attendee_visibility([event.pk])

        delay.assert_called_once_with(str(event.pk))

    def test_a_failed_enqueue_is_logged_with_its_event(
        self, event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with (
            patch(
                "events.service.series_pass_service.build_attendee_visibility_flags.delay",
                side_effect=ConnectionError("broker down"),
            ),
            patch("common.utils.logger") as log,
        ):
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.rebuild_attendee_visibility([event.pk])

        log.error.assert_called_once()
        assert log.error.call_args.args == ("visibility_flags_dispatch_failed",)
        assert log.error.call_args.kwargs["event_id"] == str(event.pk)

    def test_one_failed_enqueue_does_not_stop_the_others(
        self, event: Event, public_event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch(
            "events.service.series_pass_service.build_attendee_visibility_flags.delay",
            side_effect=[ConnectionError("broker down"), None],
        ) as delay:
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.rebuild_attendee_visibility([event.pk, public_event.pk])

        assert delay.call_count == 2


class TestReconcileSweep:
    def test_an_event_with_a_stale_count_is_rebuilt(
        self, event: Event, tier: TicketTier, member_user: RevelUser
    ) -> None:
        _bulk_ticket(event, tier, member_user)  # attendee_count stays 0: the rebuild was lost

        with patch(DELAY) as delay:
            dispatched = reconcile_attendee_counts()

        delay.assert_called_once_with(str(event.pk))
        assert dispatched == 1

    def test_an_event_with_a_correct_count_is_not_rebuilt(
        self, event: Event, tier: TicketTier, member_user: RevelUser
    ) -> None:
        _bulk_ticket(event, tier, member_user)
        Event.objects.filter(pk=event.pk).update(attendee_count=1)

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 0

        delay.assert_not_called()

    def test_yes_rsvps_count_too(self, event: Event, member_user: RevelUser) -> None:
        rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
        EventRSVP.objects.filter(pk=rsvp.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
        Event.objects.filter(pk=event.pk).update(attendee_count=1)

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 0

        delay.assert_not_called()

    def test_activity_older_than_the_window_is_not_checked(
        self, event: Event, tier: TicketTier, member_user: RevelUser
    ) -> None:
        ticket = _bulk_ticket(event, tier, member_user)
        Ticket.objects.filter(pk=ticket.pk).update(updated_at=timezone.now() - timedelta(hours=25))

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 0

        delay.assert_not_called()

    def test_activity_of_the_last_minutes_is_left_for_the_next_run(
        self, event: Event, tier: TicketTier, member_user: RevelUser
    ) -> None:
        """A rebuild that is only queued is not lost: the sweep does not dispatch it twice."""
        ticket = _bulk_ticket(event, tier, member_user)
        Ticket.objects.filter(pk=ticket.pk).update(updated_at=timezone.now() - timedelta(minutes=1))

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 0

        delay.assert_not_called()

    def test_an_rsvp_alone_brings_its_event_in(self, event: Event, member_user: RevelUser) -> None:
        rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
        EventRSVP.objects.filter(pk=rsvp.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
        Event.objects.filter(pk=event.pk).update(attendee_count=0)  # the RSVP's own rebuild was lost

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 1

        delay.assert_called_once_with(str(event.pk))

    def test_a_cancelled_ticket_is_not_counted(self, event: Event, tier: TicketTier, member_user: RevelUser) -> None:
        ticket = _bulk_ticket(event, tier, member_user)
        Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.CANCELLED)

        with patch(DELAY) as delay:
            assert reconcile_attendee_counts() == 0

        delay.assert_not_called()


def test_a_rebuild_that_fails_on_the_flags_leaves_the_count_for_the_sweep(
    event: Event, tier: TicketTier, member_user: RevelUser
) -> None:
    """The count is written only after the flags: a failed flag rebuild leaves it stale, so the sweep retries.

    Before, the count was committed first, so a failure in the flag phase left a correct count and stale flags,
    and the sweep skipped the event.
    """
    from events.models import AttendeeVisibilityFlag
    from events.tasks import build_attendee_visibility_flags

    _bulk_ticket(event, tier, member_user)
    with (
        patch.object(AttendeeVisibilityFlag.objects, "bulk_create", side_effect=RuntimeError("flags failed")),
        pytest.raises(RuntimeError),
    ):
        build_attendee_visibility_flags(str(event.pk))

    assert Event.objects.get(pk=event.pk).attendee_count == 0  # still stale
    with patch(DELAY) as delay:
        assert reconcile_attendee_counts() == 1
    delay.assert_called_once_with(str(event.pk))
