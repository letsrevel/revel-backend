"""One failing commit dispatch must not skip the dispatches after it (#1488).

Django stops the commit callbacks at the first one that raises, unless the callback is
registered with ``robust=True``. These tests make the first dispatch raise and check that
the later ones still run and that the failure is logged with its ids.
"""

import datetime as dt
import typing as t
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, Organization, Ticket, WaitlistOffer
from events.service import waitlist_service
from events.tasks import expire_waitlist_offers_task

pytestmark = pytest.mark.django_db

_TICKET_SAVE_FAILURES = [
    pytest.param(
        "events.signals.attendance.build_attendee_visibility_flags", "visibility_flags_dispatch_failed", id="visibility"
    ),
    pytest.param(
        "notifications.signals.ticket._handle_ticket_status_change", "ticket_notifications_failed", id="notifications"
    ),
    pytest.param(
        "notifications.signals.waitlist._check_and_notify_waitlist", "ticket_waitlist_logic_failed", id="waitlist"
    ),
]


def _failing_mock() -> mock.MagicMock:
    """A mock that raises when it is called, and when its ``.delay`` is called."""
    failing = mock.MagicMock(side_effect=RuntimeError("broker down"))
    failing.delay.side_effect = RuntimeError("broker down")
    return failing


@pytest.mark.parametrize(("failing_target", "failure_event"), _TICKET_SAVE_FAILURES)
def test_failing_ticket_signal_callback_does_not_skip_the_waitlist_dispatch(
    ticket: Ticket, django_capture_on_commit_callbacks: t.Any, failing_target: str, failure_event: str
) -> None:
    """A cancellation whose Ticket signal callback fails still dispatches the waitlist run."""
    with (
        mock.patch(failing_target, _failing_mock()),
        mock.patch("events.tasks.process_waitlist_for_event_task") as waitlist_task,
        mock.patch("common.utils.logger") as log,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])
            waitlist_service.enqueue_waitlist_processing(ticket.event_id)

    waitlist_task.delay.assert_called_once_with(str(ticket.event_id))
    failures = [c for c in log.error.call_args_list if c.args[0] == failure_event]
    assert len(failures) == 1
    assert failures[0].kwargs["ticket_id"] == str(ticket.pk)
    assert failures[0].kwargs["event_id"] == str(ticket.event_id)


def test_failing_visibility_dispatch_on_ticket_delete_does_not_skip_the_waitlist_check(
    ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A ticket deletion whose visibility dispatch fails still runs the waitlist check."""
    event_id = ticket.event_id
    with (
        mock.patch("events.signals.attendance.build_attendee_visibility_flags", _failing_mock()),
        mock.patch("notifications.signals.waitlist._check_and_notify_waitlist") as waitlist_check,
        mock.patch("common.utils.logger") as log,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            ticket.delete()

    waitlist_check.assert_called_once_with(event_id)
    assert [c.args[0] for c in log.error.call_args_list] == ["visibility_flags_dispatch_failed"]


def test_one_failed_dispatch_does_not_stop_the_offer_expiry_loop(
    organization: Organization,
    event: Event,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Each event with expired offers gets its next batch, also when the dispatch for another event fails."""
    other_event = Event.objects.create(organization=organization, name="Other", start=event.start, status=event.status)
    for e in (event, other_event):
        WaitlistOffer.objects.create(
            event=e,
            user=revel_user_factory(),
            expires_at=timezone.now() - dt.timedelta(minutes=1),
            batch_id=uuid.uuid4(),
        )
    waitlist_task = mock.MagicMock()
    waitlist_task.delay.side_effect = [RuntimeError("broker down"), None]
    with (
        # Both names, so the test also binds to a loop that calls the task directly.
        mock.patch("events.tasks.process_waitlist_for_event_task", waitlist_task),
        mock.patch("events.tasks.waitlist.process_waitlist_for_event_task", waitlist_task),
        mock.patch("common.utils.logger") as log,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            expire_waitlist_offers_task()

    assert {c.args[0] for c in waitlist_task.delay.call_args_list} == {str(event.id), str(other_event.id)}
    log.error.assert_called_once()
    assert log.error.call_args.args[0] == "waitlist_dispatch_failed"


def test_one_failed_offer_notification_does_not_skip_the_other_offers(
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Each offer in a batch gets its notification, also when the dispatch for another offer fails."""
    first, second = uuid.uuid4(), uuid.uuid4()
    with (
        mock.patch("events.tasks.send_waitlist_offer_notification_task") as notify_task,
        mock.patch("common.utils.logger") as log,
    ):
        notify_task.delay.side_effect = [RuntimeError("broker down"), None]
        with django_capture_on_commit_callbacks(execute=True):
            waitlist_service._dispatch_offer_notifications([first, second])
            notify_task.delay.assert_not_called()  # only after commit

    assert [c.args[0] for c in notify_task.delay.call_args_list] == [str(first), str(second)]
    log.error.assert_called_once()
    assert log.error.call_args.args[0] == "waitlist_offer_notification_dispatch_failed"
    assert log.error.call_args.kwargs["offer_id"] == str(first)


def test_failing_visibility_dispatch_after_bulk_create_does_not_skip_the_other_steps(
    ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The bulk-create side effects are independent: the notifications and the waitlist removal still run."""
    from events.service.batch_ticket_service import BatchTicketService

    service = BatchTicketService(ticket.event, ticket.tier, ticket.user)
    module = "events.service.batch_ticket_service.tickets"
    with (
        mock.patch(f"{module}.build_attendee_visibility_flags", _failing_mock()),
        mock.patch(f"{module}.send_batch_ticket_created_notifications") as notify,
        mock.patch(f"{module}.remove_user_from_waitlist") as remove,
        mock.patch("events.tasks.process_waitlist_for_event_task") as waitlist_task,
        mock.patch("common.utils.logger") as log,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            service.trigger_bulk_create_side_effects([ticket])
            waitlist_service.enqueue_waitlist_processing(ticket.event_id)

    notify.assert_called_once_with([ticket])
    remove.assert_called_once_with(ticket.event_id, ticket.user_id)
    waitlist_task.delay.assert_called_once_with(str(ticket.event_id))
    assert [c.args[0] for c in log.error.call_args_list] == ["visibility_flags_dispatch_failed"]
    assert log.error.call_args.kwargs["event_id"] == str(ticket.event_id)
