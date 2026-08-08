"""Tests for the ``timezone`` field on MinimalEventSchema (BE #862).

Dashboard lists (``/api/dashboard/tickets``, ``/api/dashboard/rsvps``) serialize
events as ``MinimalEventSchema``; exposing the resolved IANA timezone lets the
frontend render event-local times consistently with the event detail page.
"""

from datetime import timedelta

import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Organization, Ticket
from geo.models import City

pytestmark = pytest.mark.django_db


@pytest.fixture
def vienna_event(organization: Organization) -> Event:
    """An upcoming event located in Vienna (city timezone auto-derived)."""
    city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="AT",
        city_id=99001,
        location=Point(16.3738, 48.2082),
    )
    assert city.timezone == "Europe/Vienna"
    return Event.objects.create(
        organization=organization,
        name="Vienna Event",
        slug="vienna-event",
        status="open",
        city=city,
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )


def test_dashboard_tickets_expose_event_timezone(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    vienna_event: Event,
) -> None:
    """Ticket list events report the event city's IANA timezone."""
    tier = vienna_event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(guest_name="Test Guest", event=vienna_event, user=dashboard_user, tier=tier)

    response = dashboard_client.get(reverse("api:dashboard_tickets"))

    assert response.status_code == 200
    assert response.json()["results"][0]["event"]["timezone"] == "Europe/Vienna"


def test_dashboard_rsvps_expose_event_timezone(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    vienna_event: Event,
) -> None:
    """RSVP list events report the event city's IANA timezone."""
    EventRSVP.objects.create(event=vienna_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

    response = dashboard_client.get(reverse("api:dashboard_rsvps"))

    assert response.status_code == 200
    assert response.json()["results"][0]["event"]["timezone"] == "Europe/Vienna"


def test_dashboard_rsvps_timezone_falls_back_to_utc(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    organization: Organization,
) -> None:
    """Events without a city report the UTC fallback."""
    event = Event.objects.create(
        organization=organization,
        name="No City Event",
        slug="no-city-event",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )
    EventRSVP.objects.create(event=event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

    response = dashboard_client.get(reverse("api:dashboard_rsvps"))

    assert response.status_code == 200
    assert response.json()["results"][0]["event"]["timezone"] == "UTC"
