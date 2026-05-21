"""Tests for waitlist Celery tasks."""

import datetime as dt

import pytest

from conftest import RevelUserFactory
from events.models import Event, EventWaitList
from events.tasks import process_waitlist_for_event_task

pytestmark = pytest.mark.django_db


def test_process_task_delegates_to_service(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """The task wraps process_waitlist_for_event and returns its as_dict() payload."""
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.waitlist_batch_size = 0
    event.max_attendees = 10
    event.end = event.start + dt.timedelta(hours=2)
    event.save()
    Event.objects.filter(pk=event.pk).update(attendee_count=0)
    u = revel_user_factory()
    EventWaitList.objects.create(event=event, user=u)

    result = process_waitlist_for_event_task(str(event.id))
    assert result["status"] == "ok"
    assert result["offers_created"] == 1
