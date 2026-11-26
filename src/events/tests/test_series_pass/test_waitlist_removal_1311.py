"""A pass ticket removes its holder from the event waitlist, as a single ticket does (#1311).

Pass tickets are written with ``bulk_create`` and ``bulk_update``, which skip the ``post_save`` receiver that
removes a ticket holder from the waitlist. The removal follows that receiver's timing: ACTIVE always, PENDING
only for offline payment. It runs after commit.
"""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventWaitList, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db


def _waitlisted(user: RevelUser, events: t.Sequence[Event]) -> None:
    EventWaitList.objects.bulk_create([EventWaitList(event=event, user=user) for event in events])


def _waitlisted_event_ids(user: RevelUser) -> set[t.Any]:
    return set(EventWaitList.objects.filter(user=user).values_list("event_id", flat=True))


def _purchase(series_pass: SeriesPass, user: RevelUser, callbacks: t.Any) -> HeldSeriesPass:
    with patch("events.service.series_pass_purchase.send_series_pass_purchased"), callbacks(execute=True):
        held_pass = SeriesPassPurchaseService(series_pass, user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    return held_pass


def test_a_free_pass_removes_the_holder_from_the_covered_waitlists(
    series_pass: SeriesPass,
    three_upcoming: list[Event],
    revel_user: RevelUser,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    other_event, _ = cover(series_pass, "not-ticketed", days=-1)  # started: the pass gives no ticket for it
    _waitlisted(revel_user, [*three_upcoming, other_event])
    _waitlisted(member_user, three_upcoming)

    _purchase(series_pass, revel_user, django_capture_on_commit_callbacks)

    assert _waitlisted_event_ids(revel_user) == {other_event.pk}
    assert _waitlisted_event_ids(member_user) == {event.pk for event in three_upcoming}


def test_an_offline_pass_removes_the_holder_when_the_pending_tickets_are_created(
    series_pass: SeriesPass,
    three_upcoming: list[Event],
    revel_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    SeriesPass.objects.filter(pk=series_pass.pk).update(payment_method=TicketTier.PaymentMethod.OFFLINE)
    series_pass.refresh_from_db()
    _waitlisted(revel_user, three_upcoming)

    _purchase(series_pass, revel_user, django_capture_on_commit_callbacks)

    assert _waitlisted_event_ids(revel_user) == set()


def test_pending_tickets_of_an_online_pass_keep_the_holder_on_the_waitlist(
    series_pass: SeriesPass,
    three_upcoming: list[Event],
    revel_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The holder leaves the waitlist when the payment completes, as for an online single ticket."""
    SeriesPass.objects.filter(pk=series_pass.pk).update(payment_method=TicketTier.PaymentMethod.ONLINE)
    series_pass.refresh_from_db()
    held_pass = HeldSeriesPass.objects.create(series_pass=series_pass, user=revel_user, price_paid=series_pass.price)
    _waitlisted(revel_user, three_upcoming)

    with django_capture_on_commit_callbacks(execute=True):
        series_pass_service.materialize_tickets(
            held_pass, list(series_pass.tier_links.all()), Ticket.TicketStatus.PENDING
        )

    assert _waitlisted_event_ids(revel_user) == {event.pk for event in three_upcoming}


def test_the_offline_confirm_removes_the_holder(
    series_pass: SeriesPass,
    three_upcoming: list[Event],
    revel_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The tickets become ACTIVE with bulk_update: the holder leaves a waitlist joined after the purchase too."""
    SeriesPass.objects.filter(pk=series_pass.pk).update(payment_method=TicketTier.PaymentMethod.OFFLINE)
    series_pass.refresh_from_db()
    held_pass = _purchase(series_pass, revel_user, django_capture_on_commit_callbacks)
    _waitlisted(revel_user, three_upcoming)

    with (
        patch("events.service.series_pass_service.send_series_pass_purchased"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        series_pass_service.confirm_held_pass_payment(held_pass)

    assert _waitlisted_event_ids(revel_user) == set()


def test_a_backfilled_ticket_removes_the_holder(
    series_pass: SeriesPass,
    three_upcoming: list[Event],
    revel_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    held_pass = _purchase(series_pass, revel_user, django_capture_on_commit_callbacks)
    new_event, _ = cover(series_pass, "extension", days=9)
    _waitlisted(revel_user, [new_event])

    with django_capture_on_commit_callbacks(execute=True):
        series_pass_service.backfill_missing_tickets(held_pass)

    assert _waitlisted_event_ids(revel_user) == set()
