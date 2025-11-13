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
    EventInvitationRequest,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
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
    assert data["count"] == 3  # owner, staff, member
    names = {org["name"] for org in data["results"]}
    expected_names = {"Owned Org", "Staff Org", "Member Org"}
    assert names == expected_names


def test_dashboard_organizations_single_filter(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """Test filtering organizations by a single relationship type."""
    url = reverse("api:dashboard_organizations")
    response = dashboard_client.get(url, {"owner": "true", "staff": "false", "member": "false"})
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
    past_event.start = timezone.now() - timedelta(days=2)
    past_event.end = timezone.now() - timedelta(days=1)
    past_event.save()
    # Create a new, future invitation
    future_event = Event.objects.create(
        name="Future Invite Event",
        organization=dashboard_setup["orgs"]["owner"],
        status="open",
        start=timezone.now() + timedelta(days=5),
        end=timezone.now() + timedelta(days=6),
    )
    EventInvitation.objects.create(user=dashboard_setup["user"], event=future_event)

    url = reverse("api:dashboard_invitations")
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # The endpoint filters out past events (end < now)
    assert data["count"] == 1
    assert data["results"][0]["event"]["name"] == "Future Invite Event"


def test_dashboard_anonymous_user_fails(client: Client) -> None:
    """Test that an anonymous (unauthenticated) user gets a 401 from all dashboard endpoints."""
    assert client.get(reverse("api:dashboard_organizations")).status_code == 401
    assert client.get(reverse("api:dashboard_events")).status_code == 401
    assert client.get(reverse("api:dashboard_event_series")).status_code == 401
    assert client.get(reverse("api:dashboard_invitations")).status_code == 401
    assert client.get(reverse("api:dashboard_tickets")).status_code == 401
    assert client.get(reverse("api:dashboard_invitation_requests")).status_code == 401
    assert client.get(reverse("api:dashboard_rsvps")).status_code == 401


# Invitations Tests


def test_dashboard_invitations_success(
    dashboard_client: Client, dashboard_user: RevelUser, dashboard_setup: dict[str, t.Any]
) -> None:
    """Test that a user can retrieve their own invitations."""
    # The setup already creates one invitation ("Invited Event")
    # Create an additional invitation
    new_event = Event.objects.create(
        organization=dashboard_setup["orgs"]["owner"],
        name="Another Invite",
        slug="another-invite",
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
    )
    EventInvitation.objects.create(event=new_event, user=dashboard_user, custom_message="Welcome!")

    # Create an invitation for another user to ensure it's not included
    other_user = RevelUser.objects.create_user("otheruser")
    EventInvitation.objects.create(event=new_event, user=other_user)

    url = reverse("api:dashboard_invitations")
    response = dashboard_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2  # The existing one + new one
    # Verify event information is included
    result_event_names = {r["event"]["name"] for r in data["results"]}
    assert "Another Invite" in result_event_names


def test_dashboard_invitations_filter_by_upcoming(
    dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
) -> None:
    """Test that by default only upcoming event invitations are shown."""
    now = timezone.now()

    # Create past event (ended 2 days ago)
    past_event = Event.objects.create(
        organization=organization,
        name="Past Event",
        slug="past-event",
        status="open",
        start=now - timedelta(days=3),
        end=now - timedelta(days=2),
    )

    # Create upcoming event (starts in 1 week)
    upcoming_event = Event.objects.create(
        organization=organization,
        name="Upcoming Event",
        slug="upcoming-event",
        status="open",
        start=now + timedelta(days=7),
        end=now + timedelta(days=8),
    )

    # Create another past event (ended 1 hour ago)
    another_past_event = Event.objects.create(
        organization=organization,
        name="Another Past Event",
        slug="another-past-event",
        status="open",
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),
    )

    # Create invitations for all events
    EventInvitation.objects.create(event=past_event, user=dashboard_user)
    upcoming_invitation = EventInvitation.objects.create(event=upcoming_event, user=dashboard_user)
    EventInvitation.objects.create(event=another_past_event, user=dashboard_user)

    url = reverse("api:dashboard_invitations")

    # Default should show only upcoming
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # Only the upcoming event invitation should be returned
    result_ids = {r["id"] for r in data["results"]}
    assert result_ids == {str(upcoming_invitation.id)}, f"Expected only upcoming invitation, got: {data['results']}"
    assert data["count"] == 1

    # With include_past=true should show all
    response = dashboard_client.get(url, {"include_past": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3


def test_dashboard_invitations_filter_by_event(
    dashboard_client: Client, dashboard_user: RevelUser, dashboard_setup: dict[str, t.Any]
) -> None:
    """Test filtering invitations by event_id."""
    event1 = Event.objects.create(
        organization=dashboard_setup["orgs"]["owner"],
        name="Event One",
        slug="event-one",
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
    )
    event2 = Event.objects.create(
        organization=dashboard_setup["orgs"]["owner"],
        name="Event Two",
        slug="event-two",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )

    invitation1 = EventInvitation.objects.create(event=event1, user=dashboard_user)
    EventInvitation.objects.create(event=event2, user=dashboard_user)

    url = reverse("api:dashboard_invitations")

    # Filter by event1
    response = dashboard_client.get(url, {"event_id": str(event1.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(invitation1.id)
    assert data["results"][0]["event"]["id"] == str(event1.id)


def test_dashboard_invitations_search(
    dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
) -> None:
    """Test searching invitations by event name/description and custom message."""
    event1 = Event.objects.create(
        organization=organization,
        name="Tech Meetup",
        slug="tech-meetup",
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
    )
    event2 = Event.objects.create(
        organization=organization,
        name="Art Gallery",
        slug="art-gallery",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
        description="Beautiful art show",
    )

    invitation1 = EventInvitation.objects.create(event=event1, user=dashboard_user, custom_message="Tech enthusiast")
    invitation2 = EventInvitation.objects.create(event=event2, user=dashboard_user)

    url = reverse("api:dashboard_invitations")

    # Search by event name
    response = dashboard_client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation1.id)

    # Search by custom message
    response = dashboard_client.get(url, {"search": "enthusiast"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation1.id)

    # Search by event description
    response = dashboard_client.get(url, {"search": "Beautiful"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation2.id)


# Tickets Tests


def test_dashboard_tickets(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    dashboard_setup: dict[str, t.Any],
) -> None:
    """Test listing user's own tickets with filtering and search."""
    # The setup already created one ticket for "Ticketed Event"
    # Create another ticket with different status
    new_event = Event.objects.create(
        organization=dashboard_setup["orgs"]["owner"],
        name="Another Ticketed Event",
        slug="another-ticketed",
        status="open",
        start=timezone.now() + timedelta(days=5),
        end=timezone.now() + timedelta(days=6),
    )
    tier = new_event.ticket_tiers.first()
    assert tier is not None
    ticket2 = Ticket.objects.create(event=new_event, user=dashboard_user, tier=tier, status=Ticket.TicketStatus.PENDING)

    url = reverse("api:dashboard_tickets")

    # Get all tickets (no filter)
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    # Filter by status
    response = dashboard_client.get(url, {"status": "pending"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ticket2.id)
    assert data["results"][0]["event"]["name"] == "Another Ticketed Event"

    # Search by event name
    response = dashboard_client.get(url, {"search": "Another Ticketed"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ticket2.id)


def test_dashboard_tickets_include_past(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    organization: Organization,
) -> None:
    """Test filtering tickets by past/upcoming events."""
    # Create past event
    past_event = Event.objects.create(
        organization=organization,
        name="Past Ticketed Event",
        slug="past-ticketed",
        status="open",
        start=timezone.now() - timedelta(days=3),
        end=timezone.now() - timedelta(days=2),
    )
    tier_past = past_event.ticket_tiers.first()
    assert tier_past is not None
    Ticket.objects.create(event=past_event, user=dashboard_user, tier=tier_past)

    # Create upcoming event
    upcoming_event = Event.objects.create(
        organization=organization,
        name="Upcoming Ticketed Event",
        slug="upcoming-ticketed",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )
    tier_upcoming = upcoming_event.ticket_tiers.first()
    assert tier_upcoming is not None
    upcoming_ticket = Ticket.objects.create(event=upcoming_event, user=dashboard_user, tier=tier_upcoming)

    url = reverse("api:dashboard_tickets")

    # Default: only upcoming
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(upcoming_ticket.id)

    # With include_past=true
    response = dashboard_client.get(url, {"include_past": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2


# Invitation Requests Tests


def test_dashboard_invitation_requests(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    organization: Organization,
) -> None:
    """Test listing user's invitation requests."""
    event1 = Event.objects.create(
        organization=organization,
        name="Event One",
        slug="event-one",
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
    )
    event2 = Event.objects.create(
        organization=organization,
        name="Event Two",
        slug="event-two",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )

    # Create requests with different statuses
    req1 = EventInvitationRequest.objects.create(
        event=event1, user=dashboard_user, status=EventInvitationRequest.InvitationRequestStatus.PENDING
    )
    req2 = EventInvitationRequest.objects.create(
        event=event2, user=dashboard_user, status=EventInvitationRequest.InvitationRequestStatus.APPROVED
    )

    url = reverse("api:dashboard_invitation_requests")

    # Default: only pending
    response = dashboard_client.get(url, {"status": "pending"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(req1.id)

    # All statuses
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    # Filter by event
    response = dashboard_client.get(url, {"event_id": str(event2.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(req2.id)


# RSVPs Tests


def test_dashboard_rsvps(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    dashboard_setup: dict[str, t.Any],
) -> None:
    """Test listing user's RSVPs."""
    # The setup already created one RSVP ("RSVP'd Event" with status "yes")
    # Create another RSVP with different status
    new_event = Event.objects.create(
        organization=dashboard_setup["orgs"]["owner"],
        name="Maybe Event",
        slug="maybe-event",
        status="open",
        start=timezone.now() + timedelta(days=5),
        end=timezone.now() + timedelta(days=6),
    )
    rsvp2 = EventRSVP.objects.create(event=new_event, user=dashboard_user, status=EventRSVP.RsvpStatus.MAYBE)

    url = reverse("api:dashboard_rsvps")

    # Get all RSVPs
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    # Filter by status
    response = dashboard_client.get(url, {"status": "maybe"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp2.id)
    assert data["results"][0]["event"]["name"] == "Maybe Event"

    # Search by event name
    response = dashboard_client.get(url, {"search": "Maybe"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp2.id)


def test_dashboard_rsvps_include_past(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    organization: Organization,
) -> None:
    """Test filtering RSVPs by past/upcoming events."""
    # Create past event
    past_event = Event.objects.create(
        organization=organization,
        name="Past RSVP Event",
        slug="past-rsvp",
        status="open",
        start=timezone.now() - timedelta(days=3),
        end=timezone.now() - timedelta(days=2),
    )
    EventRSVP.objects.create(event=past_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

    # Create upcoming event
    upcoming_event = Event.objects.create(
        organization=organization,
        name="Upcoming RSVP Event",
        slug="upcoming-rsvp",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )
    upcoming_rsvp = EventRSVP.objects.create(event=upcoming_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:dashboard_rsvps")

    # Default: only upcoming
    response = dashboard_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(upcoming_rsvp.id)

    # With include_past=true
    response = dashboard_client.get(url, {"include_past": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
