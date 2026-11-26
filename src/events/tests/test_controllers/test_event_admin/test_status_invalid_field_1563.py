"""A status change on an event that an earlier edit left invalid names the field to fix (#1563).

The save of a status change validates only the changed fields, but ``Event.clean()`` still checks the whole
row. A cross-field rule that an earlier edit broke therefore blocks the change: fix the data first.
"""

from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event

pytestmark = pytest.mark.django_db


def test_a_status_change_on_an_event_with_an_invalid_field_names_the_field(
    organization_owner_client: Client, event: Event
) -> None:
    """A cross-field rule broken by an earlier edit blocks a status change, and the 400 names the field to fix."""
    Event.objects.filter(pk=event.pk).update(
        status=Event.EventStatus.OPEN,
        waitlist_time_window=timedelta(hours=1),
        waitlist_cutoff_date=event.start + timedelta(days=1),
    )
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CLOSED})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400, response.content
    assert "waitlist_cutoff_date" in response.content.decode()
    assert Event.objects.get(pk=event.pk).status == Event.EventStatus.OPEN
