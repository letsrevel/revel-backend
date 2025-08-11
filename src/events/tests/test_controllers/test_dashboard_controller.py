import typing as t
from datetime import timedelta

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
    UserOrganizationPreferences,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def dashboard_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user for whom we'll test the dashboard."""
    return django_user_model.objects.create_user(username="dash", email="dash@example.com", password="p")


@pytest.fixture
def dashboard_client(dashboard_user: RevelUser) -> Client:
    """An authenticated client for the dashboard user."""
    refresh = RefreshToken.for_user(dashboard_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def dashboard_setup(dashboard_user: RevelUser) -> t.Dict[str, t.Any]:
    """Creates a complex set of related objects for dashboard testing."""
    # Orgs with direct relationship
    org_owner = Organization.objects.create(name="Owned Org", owner=dashboard_user)
    org_staff = Organization.objects.create(name="Staff Org", owner=RevelUser.objects.create_user("anotherowner"))
    OrganizationStaff.objects.create(organization=org_staff, user=dashboard_user)
    org_member = Organization.objects.create(name="Member Org", owner=RevelUser.objects.create_user("thirdowner"))
    OrganizationMember.objects.create(organization=org_member, user=dashboard_user)

    # Orgs with indirect (event/sub) relationship
    org_public_rsvp = Organization.objects.create(
        name="RSVP Org", owner=RevelUser.objects.create_user("fourthowner"), visibility="public"
    )
    org_public_ticket = Organization.objects.create(
        name="Ticket Org", owner=RevelUser.objects.create_user("fifthowner"), visibility="public"
    )
    org_public_sub = Organization.objects.create(
        name="Subscribed Org", owner=RevelUser.objects.create_user("sixthowner"), visibility="public"
    )
    UserOrganizationPreferences.objects.create(organization=org_public_sub, user=dashboard_user, is_subscribed=True)

    # A private org the user has no access to
    org_private_unrelated = Organization.objects.create(
        name="Unrelated Private Org", owner=RevelUser.objects.create_user("seventhowner"), visibility="private"
    )

    # Events
    evt_owner = Event.objects.create(name="In Owned Org", organization=org_owner, status="open", start=timezone.now())
    evt_staff = Event.objects.create(name="In Staff Org", organization=org_staff, status="open", start=timezone.now())
    evt_member = Event.objects.create(
        name="In Member Org",
        organization=org_member,
        status="open",
        visibility=Event.Visibility.MEMBERS_ONLY,
        start=timezone.now(),
    )
    evt_rsvp = Event.objects.create(
        name="RSVP'd Event", organization=org_public_rsvp, status="open", start=timezone.now()
    )
    EventRSVP.objects.create(event=evt_rsvp, user=dashboard_user, status="yes")
    evt_ticket = Event.objects.create(
        name="Ticketed Event", organization=org_public_ticket, status="open", start=timezone.now()
    )
    tier = evt_ticket.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(event=evt_ticket, user=dashboard_user, tier=tier)
    evt_invite = Event.objects.create(
        name="Invited Event", organization=org_public_ticket, status="open", start=timezone.now()
    )  # another event in a public org
    EventInvitation.objects.create(event=evt_invite, user=dashboard_user)
    # This event is in a private org and user has no relation, so it shouldn't appear
    Event.objects.create(
        name="Unrelated Private Event", organization=org_private_unrelated, status="open", start=timezone.now()
    )

    return {
        "user": dashboard_user,
        "orgs": {
            "owner": org_owner,
            "staff": org_staff,
            "member": org_member,
            "rsvp": org_public_rsvp,
            "ticket": org_public_ticket,
            "sub": org_public_sub,
            "private": org_private_unrelated,
        },
        "events": {
            "owner": evt_owner,
            "staff": evt_staff,
            "member": evt_member,
            "rsvp": evt_rsvp,
            "ticket": evt_ticket,
            "invite": evt_invite,
        },
    }


def test_dashboard_organizations_default_filters(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test the orgs endpoint with default filters (all true). Should return all related orgs."""
    url = reverse("api:dashboard_organizations")
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 4  # owner, staff, member, subscribed
    names = {org["name"] for org in data["results"]}
    expected_names = {"Owned Org", "Staff Org", "Member Org", "Subscribed Org"}
    assert names == expected_names


def test_dashboard_organizations_single_filter(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test filtering organizations by a single relationship type."""
    url = reverse("api:dashboard_organizations")
    response = dashboard_client.get(url, {"owner": "true", "staff": "false", "member": "false", "subscriber": "false"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["name"] == "Owned Org"


def test_dashboard_events_default_filters(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test the events endpoint with default filters. Should return all related events."""
    url = reverse("api:dashboard_events")
    # By default: rsvp_no is false, others are true
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # owner, staff, member, rsvp_yes, got_ticket, got_invitation are the relationships
    assert data["count"] == 6
    names = {evt["name"] for evt in data["results"]}
    expected_names = {
        "In Owned Org",
        "In Staff Org",
        "In Member Org",
        "RSVP'd Event",
        "Ticketed Event",
        "Invited Event",
    }
    assert names == expected_names


def test_dashboard_events_filtered_by_ticket(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test filtering events to only those where the user has a ticket."""
    url = reverse("api:dashboard_events")
    params = {
        "owner": "false",
        "staff": "false",
        "member": "false",
        "rsvp_yes": "false",
        "rsvp_maybe": "false",
        "got_invitation": "false",
        "subscriber": "false",
        "got_ticket": "true",  # The only active filter
    }
    response = dashboard_client.get(url, params)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["name"] == "Ticketed Event"


def test_dashboard_invitations(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test the invitations endpoint."""
    # Setup: one of the events is in the past
    past_event = dashboard_setup["events"]["invite"]
    past_event.start = timezone.now() - timedelta(days=1)
    past_event.save()
    # Create a new, future invitation
    future_event = Event.objects.create(
        name="Future Invite Event",
        organization=dashboard_setup["orgs"]["owner"],
        status="open",
        start=timezone.now() + timedelta(days=5),
    )
    EventInvitation.objects.create(user=dashboard_setup["user"], event=future_event)

    url = reverse("api:dashboard_invitations")
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # The `for_user` manager method on EventInvitation filters out past events
    assert data["count"] == 1
    assert data["results"][0]["event"]["name"] == "Future Invite Event"


def test_dashboard_anonymous_user_fails(client: Client) -> None:
    """Test that an anonymous (unauthenticated) user gets a 401 from all dashboard endpoints."""
    assert client.get(reverse("api:dashboard_organizations")).status_code == 401
    assert client.get(reverse("api:dashboard_events")).status_code == 401
    assert client.get(reverse("api:dashboard_event_series")).status_code == 401
    assert client.get(reverse("api:dashboard_invitations")).status_code == 401
