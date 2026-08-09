"""update_status refund_tickets flag + un-cancel guard."""

import typing as t
from unittest.mock import patch

import pytest
from django.utils import timezone

from events.exceptions import EventRefundsStartedError
from events.models import Event
from events.service import event_service

pytestmark = pytest.mark.django_db


def test_cancel_without_flag_leaves_stamp_null_and_dispatches_nothing(
    event: t.Any, django_capture_on_commit_callbacks: t.Any
) -> None:
    with patch("events.tasks.refunds.refund_cancelled_event_tickets.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.update_status(event, Event.EventStatus.CANCELLED)
    event.refresh_from_db()
    assert event.tickets_refund_started_at is None
    mock_delay.assert_not_called()


def test_cancel_with_flag_stamps_and_dispatches_on_commit(
    event: t.Any, organization_owner_user: t.Any, django_capture_on_commit_callbacks: t.Any
) -> None:
    with patch("events.tasks.refunds.refund_cancelled_event_tickets.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.update_status(
                event,
                Event.EventStatus.CANCELLED,
                refund_tickets=True,
                initiated_by=organization_owner_user,
            )
    event.refresh_from_db()
    assert event.tickets_refund_started_at is not None
    mock_delay.assert_called_once_with(str(event.id), str(organization_owner_user.id))


def test_uncancel_blocked_after_refunds_started(event: t.Any) -> None:
    event.status = Event.EventStatus.CANCELLED
    event.tickets_refund_started_at = timezone.now()
    event.save(update_fields=["status", "tickets_refund_started_at"])
    with pytest.raises(EventRefundsStartedError):
        event_service.update_status(event, Event.EventStatus.OPEN)


def test_flag_ignored_for_non_cancel_target(event: t.Any) -> None:
    # The controller rejects it with 400; the service simply ignores it.
    with patch("events.tasks.refunds.refund_cancelled_event_tickets.delay") as mock_delay:
        event_service.update_status(event, Event.EventStatus.OPEN, refund_tickets=True)
    mock_delay.assert_not_called()


def test_refund_stamp_not_in_public_event_schemas() -> None:
    from events.schema import EventDetailSchema, MinimalEventSchema

    assert "tickets_refund_started_at" not in EventDetailSchema.model_fields
    assert "tickets_refund_started_at" not in MinimalEventSchema.model_fields
