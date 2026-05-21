"""Tests for nudge_open_waitlists_task."""

import datetime as dt
from unittest import mock

import pytest

from events.models import Event
from events.tasks import nudge_open_waitlists_task

pytestmark = pytest.mark.django_db


def test_nudges_only_events_with_active_advanced_waitlist(event: Event) -> None:
    """Only events with ``waitlist_open=True`` and a non-null
    ``waitlist_time_window`` should be nudged; everything else is skipped.
    """
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()

    # Make a second event with waitlist OFF — it must not be nudged.
    other = Event.objects.create(
        organization=event.organization,
        name="No waitlist",
        slug="no-waitlist",
        start=event.start,
        end=event.end,
        waitlist_open=False,
        waitlist_time_window=None,
    )

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        result = nudge_open_waitlists_task()

    assert result == {"events_nudged": 1}
    enqueue_mock.assert_called_once_with(event.id)
    # Sanity: the other event was filtered out
    assert other.id not in {call.args[0] for call in enqueue_mock.call_args_list}


def test_skips_events_with_closed_waitlist(event: Event) -> None:
    """Even when a time window is configured, ``waitlist_open=False`` skips nudge."""
    event.waitlist_open = False
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        result = nudge_open_waitlists_task()

    assert result == {"events_nudged": 0}
    enqueue_mock.assert_not_called()


def test_skips_events_with_legacy_passive_waitlist(event: Event) -> None:
    """``waitlist_open=True`` but ``waitlist_time_window=None`` = legacy passive mode."""
    event.waitlist_open = True
    event.waitlist_time_window = None
    event.save()

    with mock.patch("events.service.waitlist_service.enqueue_waitlist_processing") as enqueue_mock:
        result = nudge_open_waitlists_task()

    assert result == {"events_nudged": 0}
    enqueue_mock.assert_not_called()
