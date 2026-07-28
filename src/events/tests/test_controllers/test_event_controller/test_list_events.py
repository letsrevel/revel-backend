"""Tests for GET /events/ endpoint."""

from datetime import datetime, timedelta

import pytest
from django.test.client import Client
from django.urls import reverse

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


@pytest.fixture
def other_organization() -> Organization:
    """A second organization, so slug filtering has something to exclude."""
    owner = RevelUser.objects.create_user("other_org_owner", email="other-org-owner@example.com")
    return Organization.objects.create(name="Other Org", slug="other-org", owner=owner)


def _make_event(
    organization: Organization,
    name: str,
    slug: str,
    start: datetime,
    visibility: Event.Visibility = Event.Visibility.PUBLIC,
    event_type: Event.EventType = Event.EventType.PUBLIC,
) -> Event:
    return Event.objects.create(
        name=name,
        slug=slug,
        organization=organization,
        visibility=visibility,
        event_type=event_type,
        status=Event.EventStatus.OPEN,
        start=start,
        end=start + timedelta(days=1),
    )


def test_list_events_filter_by_organization_slug(
    client: Client,
    organization: Organization,
    other_organization: Organization,
    next_week: datetime,
) -> None:
    """Filtering by organization_slug returns only that organization's events."""
    ours = _make_event(organization, "Our Event", "our-event", next_week)
    _make_event(other_organization, "Their Event", "their-event", next_week)

    response = client.get(reverse("api:list_events"), {"organization_slug": organization.slug})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ours.id)


def test_list_events_filter_by_organization_slug_and_id(
    client: Client,
    organization: Organization,
    other_organization: Organization,
    next_week: datetime,
) -> None:
    """organization_slug and organization compose with AND, not OR."""
    ours = _make_event(organization, "Our Event", "our-event", next_week)
    _make_event(other_organization, "Their Event", "their-event", next_week)
    url = reverse("api:list_events")

    # Consistent pair: the event is returned.
    response = client.get(url, {"organization_slug": organization.slug, "organization": str(organization.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ours.id)

    # Contradictory pair: ANDed, so nothing matches (an OR would leak both events).
    response = client.get(url, {"organization_slug": organization.slug, "organization": str(other_organization.id)})
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_list_events_filter_by_unknown_organization_slug(
    client: Client, organization: Organization, next_week: datetime
) -> None:
    """An unknown slug yields an empty page, not an error."""
    _make_event(organization, "Our Event", "our-event", next_week)

    response = client.get(reverse("api:list_events"), {"organization_slug": "does-not-exist"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert data["results"] == []


def test_list_events_filter_by_organization_slug_respects_visibility(
    client: Client,
    member_client: Client,
    organization: Organization,
    next_week: datetime,
) -> None:
    """The slug filter narrows results without widening visibility."""
    public_evt = _make_event(organization, "Public Party", "public-party", next_week)
    _make_event(
        organization,
        "Private Affair",
        "private-affair",
        next_week,
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
    )
    members_evt = _make_event(
        organization,
        "Members Gala",
        "members-gala",
        next_week,
        visibility=Event.Visibility.MEMBERS_ONLY,
        event_type=Event.EventType.MEMBERS_ONLY,
    )
    url = reverse("api:list_events")
    params = {"organization_slug": organization.slug}

    # Anonymous: only the public event, private and members-only stay hidden.
    response = client.get(url, params)
    assert response.status_code == 200
    assert {evt["name"] for evt in response.json()["results"]} == {public_evt.name}

    # Member: also sees the members-only event, still never the private one.
    response = member_client.get(url, params)
    assert response.status_code == 200
    assert {evt["name"] for evt in response.json()["results"]} == {public_evt.name, members_evt.name}


def test_calendar_events_filter_by_organization_slug(
    client: Client,
    organization: Organization,
    other_organization: Organization,
    next_week: datetime,
) -> None:
    """The calendar endpoint shares EventFilterSchema, so the slug filter applies there too."""
    ours = _make_event(organization, "Our Event", "our-event", next_week)
    _make_event(other_organization, "Their Event", "their-event", next_week)

    response = client.get(
        reverse("api:calendar_events"),
        {
            "organization_slug": organization.slug,
            "month": str(next_week.month),
            "year": str(next_week.year),
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert [evt["id"] for evt in data] == [str(ours.id)]


def test_list_events_search_no_duplicates_with_tags(
    client: Client, organization: Organization, next_week: datetime
) -> None:
    """A tagged event must appear once when searched, not once per tag (regression for #664)."""
    event = Event.objects.create(
        name="Summer Sunset Music Festival",
        slug="sunset",
        organization=organization,
        visibility="public",
        event_type=Event.EventType.PUBLIC,
        description="Open-air music.",
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    event.add_tags("music", "festival", "summer")

    url = reverse("api:list_events")
    response = client.get(url, {"search": "Sunset"})

    assert response.status_code == 200
    results = response.json()["results"]
    ids = [r["id"] for r in results]
    assert ids == [str(event.id)]
    assert len(ids) == len(set(ids))
