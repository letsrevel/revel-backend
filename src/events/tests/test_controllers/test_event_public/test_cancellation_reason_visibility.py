"""Tests that the event detail endpoint only discloses cancellation_reason to attendees."""

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventRSVP

pytestmark = pytest.mark.django_db


@pytest.fixture
def cancelled_public_event(public_event: Event) -> Event:
    """The shared public event, cancelled with a reason."""
    public_event.status = Event.EventStatus.CANCELLED
    public_event.cancellation_reason = "Venue flooded"
    public_event.save(update_fields=["status", "cancellation_reason"])
    return public_event


def test_non_attending_user_does_not_see_reason(nonmember_client: Client, cancelled_public_event: Event) -> None:
    """A user with no ticket/RSVP gets null even though the reason is set."""
    url = reverse("api:get_event", kwargs={"event_id": str(cancelled_public_event.id)})

    response = nonmember_client.get(url)

    assert response.status_code == 200
    assert response.json()["cancellation_reason"] is None


def test_attending_user_sees_reason(
    nonmember_client: Client, nonmember_user: RevelUser, cancelled_public_event: Event
) -> None:
    """A user with a confirmed RSVP sees the cancellation reason."""
    EventRSVP.objects.create(user=nonmember_user, event=cancelled_public_event, status=EventRSVP.RsvpStatus.YES)
    url = reverse("api:get_event", kwargs={"event_id": str(cancelled_public_event.id)})

    response = nonmember_client.get(url)

    assert response.status_code == 200
    assert response.json()["cancellation_reason"] == "Venue flooded"
