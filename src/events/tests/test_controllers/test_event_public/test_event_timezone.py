"""Tests for the event ``timezone`` field exposed on event schemas (BE #548).

The frontend renders event datetimes in the event's own timezone. The backend
exposes the resolved IANA timezone (matching ``get_event_timezone`` / the email
formatting) so the FE never has to re-derive tz from coordinates.
"""

import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from django.urls import reverse

from events.models import Event
from geo.models import City

pytestmark = pytest.mark.django_db


def test_timezone_falls_back_to_utc_without_city(public_event: Event) -> None:
    """An event without a city reports the UTC fallback."""
    url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

    response = Client().get(url)

    assert response.status_code == 200
    assert response.json()["timezone"] == "UTC"


def test_timezone_reflects_event_city(public_event: Event) -> None:
    """An event in a city reports that city's IANA timezone."""
    # City.save() auto-populates timezone from the coordinates (Vienna).
    public_event.city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="AT",
        city_id=99001,
        location=Point(16.3738, 48.2082),
    )
    public_event.save(update_fields=["city"])
    assert public_event.city.timezone == "Europe/Vienna"

    url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

    response = Client().get(url)

    assert response.status_code == 200
    assert response.json()["timezone"] == "Europe/Vienna"
