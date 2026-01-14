"""Tests for GET /events/ endpoint."""

from datetime import datetime, timedelta

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventSeries,
    Organization,
)

pytestmark = pytest.mark.django_db


def test_list_events_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    organization_staff_client: Client,
    organization_owner_client: Client,
    superuser_client: Client,
    organization: Organization,
    nonmember_user: RevelUser,
    next_week: datetime,
) -> None:
    """Test that the event list endpoint respects user visibility rules."""
    # --- Setup ---
    # 1. Create a variety of events within the main organization
    public_evt = Event.objects.create(
        name="Public Party",
        slug="public-party",
        organization=organization,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    private_evt = Event.objects.create(
        name="Private Affair",
        slug="private-affair",
        organization=organization,
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    members_only_evt = Event.objects.create(
        name="Members Gala",
        slug="members-gala",
        organization=organization,
        visibility=Event.Visibility.MEMBERS_ONLY,
        event_type=Event.EventType.MEMBERS_ONLY,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )

    # 2. Invite the 'nonmember_user' to the private event. They become an "invited user".
    EventInvitation.objects.create(user=nonmember_user, event=private_evt)

    # 3. Create an event in a completely different org to test scoping
    other_org_owner = RevelUser.objects.create_user("otherowner")
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=other_org_owner)
    other_org_evt = Event.objects.create(
        name="External Event",
        slug="external-event",
        organization=other_org,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )

    url = reverse("api:list_events")

    # --- Assertions ---
    # Anonymous client: sees only public events
    response = client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, other_org_evt.name}

    # Invited client (was non-member): sees public events + the private one they're invited to
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, private_evt.name, other_org_evt.name}

    # Member client: sees public events + members-only events
    response = member_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, members_only_evt.name, other_org_evt.name}

    # Staff & Owner clients: see all events in their organization + all public events
    for c in [organization_staff_client, organization_owner_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 4
        names = {evt["name"] for evt in data["results"]}
        assert names == {public_evt.name, private_evt.name, members_only_evt.name, other_org_evt.name}

    # Superuser client: sees everything
    response = superuser_client.get(url)
    assert response.status_code == 200
    assert response.json()["count"] == 4


def test_list_events_search(
    client: Client, organization: Organization, event_series: EventSeries, next_week: datetime
) -> None:
    """Test searching for events by name, description, series, and organization."""
    Event.objects.create(
        name="Tech Conference",
        slug="tech",
        organization=organization,
        visibility="public",
        event_type=Event.EventType.PUBLIC,
        description="A conference about Python.",
        event_series=event_series,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    Event.objects.create(
        name="Art Fair",
        slug="art",
        organization=organization,
        visibility="public",
        event_type=Event.EventType.PUBLIC,
        description="A fair for artists using generative AI.",
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    url = reverse("api:list_events")

    # Search by event name
    response = client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Tech Conference"

    # Search by event description
    response = client.get(url, {"search": "generative AI"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Art Fair"

    # Search by event series name
    response = client.get(url, {"search": event_series.name})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
    assert response.json()["results"][0]["name"] == "Tech Conference"

    # Search by organization name
    response = client.get(url, {"search": organization.name})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 2

    # No results
    response = client.get(url, {"search": "nonexistent"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 0
