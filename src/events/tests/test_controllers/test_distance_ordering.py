"""Tie-breaks for the distance ordering of event and organization lists (#1516)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.gis.geos import Point
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from common.controllers.base import UserAwareController
from events.models import Event, Organization

pytestmark = pytest.mark.django_db

BERLIN = Point(13.405, 52.52, srid=4326)
VIENNA = Point(16.3738, 48.2082, srid=4326)


def _public_events_at(organization: Organization, location: Point, days: list[int]) -> list[Event]:
    """Create public events at one location, in the given order of days from now."""
    now = timezone.now()
    return [
        Event.objects.create(
            organization=organization,
            name=f"Event {day}",
            slug=f"event-{day}-{int(location.x)}",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=now + timedelta(days=day),
            location=location,
        )
        for day in days
    ]


@pytest.fixture
def public_organization(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()
    return organization


def _event_ids(client: Client, location: Point | None, **params: str) -> list[str]:
    with patch.object(UserAwareController, "user_location", return_value=location):
        response = client.get(reverse("api:list_events"), params)
    assert response.status_code == 200
    return [e["id"] for e in response.json()["results"]]


class TestEventDistanceOrdering:
    def test_events_at_the_same_distance_come_back_soonest_first(
        self, client: Client, public_organization: Organization
    ) -> None:
        """Nearest First orders same-place events by start, after the nearer place."""
        # Created latest-first, so the physical row order is the wrong answer.
        berlin = _public_events_at(public_organization, BERLIN, [30, 10, 20])
        vienna = _public_events_at(public_organization, VIENNA, [3, 1, 2])

        ids = _event_ids(client, BERLIN, order_by="distance")

        expected = sorted(berlin, key=lambda e: e.start) + sorted(vienna, key=lambda e: e.start)
        assert ids == [str(e.id) for e in expected]

    def test_events_without_a_user_location_come_back_soonest_first(
        self, client: Client, public_organization: Organization
    ) -> None:
        """With no user location, the distance ordering falls back to start, not to no order."""
        events = _public_events_at(public_organization, BERLIN, [30, 10, 20])

        ids = _event_ids(client, None, order_by="distance")

        assert ids == [str(e.id) for e in sorted(events, key=lambda e: e.start)]

    def test_events_with_the_same_start_have_a_stable_order(
        self, client: Client, public_organization: Organization
    ) -> None:
        """Events that tie on start are ordered by id, so pages do not repeat or skip rows."""
        events = _public_events_at(public_organization, BERLIN, [5, 6, 7])
        Event.objects.filter(pk__in=[e.pk for e in events]).update(start=events[0].start)

        for order_by in ("distance", "start", "-start"):
            assert _event_ids(client, BERLIN, order_by=order_by) == sorted(str(e.id) for e in events)


class TestOrganizationDistanceOrdering:
    def test_organizations_at_the_same_distance_come_back_by_name(
        self, client: Client, organization_owner_user: RevelUser
    ) -> None:
        """Nearest First orders same-place organizations by name; no location falls back to name."""
        names = ["Charlie", "Alpha", "Bravo"]
        for name in names:
            Organization.objects.create(
                name=name,
                slug=name.lower(),
                owner=organization_owner_user,
                visibility=Organization.Visibility.PUBLIC,
                location=BERLIN,
            )

        for location in (BERLIN, None):
            with patch.object(UserAwareController, "user_location", return_value=location):
                response = client.get(reverse("api:list_organizations"), {"order_by": "distance"})
            assert response.status_code == 200
            assert [o["name"] for o in response.json()["results"]] == sorted(names)
