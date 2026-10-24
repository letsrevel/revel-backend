"""Tests for the optional ``organization`` filter on the attendee "my" list endpoints (#1154).

Each endpoint gets a narrowing test (filter by one organization) and a no-filter test
(all items returned). The dashboard endpoints reuse ``dashboard_setup`` from conftest.py.
"""

import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    EventInvitation,
    EventInvitationRequest,
    EventRSVP,
    EventSeries,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    Ticket,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db


def _add_ticket(dashboard_setup: dict[str, t.Any], event_key: str) -> None:
    """Give the dashboard user a second ticket, in the organization of ``event_key``."""
    event = dashboard_setup["events"][event_key]
    tier = event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(guest_name="Second Guest", event=event, user=dashboard_setup["user"], tier=tier)


# ---- /dashboard/tickets ----


def test_dashboard_tickets_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    _add_ticket(dashboard_setup, "owner")
    url = reverse("api:dashboard_tickets")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["ticket"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["event"]["id"] == str(dashboard_setup["events"]["ticket"].id)


def test_dashboard_tickets_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    _add_ticket(dashboard_setup, "owner")
    response = dashboard_client.get(reverse("api:dashboard_tickets"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


# ---- /dashboard/rsvps ----


def test_dashboard_rsvps_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventRSVP.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"], status="yes")
    url = reverse("api:dashboard_rsvps")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["owner"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["event"]["id"] == str(dashboard_setup["events"]["owner"].id)


def test_dashboard_rsvps_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventRSVP.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"], status="yes")
    response = dashboard_client.get(reverse("api:dashboard_rsvps"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


# ---- /dashboard/invitations ----


def test_dashboard_invitations_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventInvitation.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"])
    url = reverse("api:dashboard_invitations")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["ticket"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["event"]["id"] == str(dashboard_setup["events"]["invite"].id)


def test_dashboard_invitations_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventInvitation.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"])
    response = dashboard_client.get(reverse("api:dashboard_invitations"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


# ---- /dashboard/invitation-requests ----


def test_dashboard_invitation_requests_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventInvitationRequest.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"])
    EventInvitationRequest.objects.create(event=dashboard_setup["events"]["staff"], user=dashboard_setup["user"])
    url = reverse("api:dashboard_invitation_requests")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["owner"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["event"]["id"] == str(dashboard_setup["events"]["owner"].id)


def test_dashboard_invitation_requests_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    EventInvitationRequest.objects.create(event=dashboard_setup["events"]["owner"], user=dashboard_setup["user"])
    EventInvitationRequest.objects.create(event=dashboard_setup["events"]["staff"], user=dashboard_setup["user"])
    response = dashboard_client.get(reverse("api:dashboard_invitation_requests"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


# ---- /dashboard/events and /dashboard/calendar ----


def test_dashboard_events_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    url = reverse("api:dashboard_events")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["owner"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["name"] == "In Owned Org"


def test_dashboard_events_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    response = dashboard_client.get(reverse("api:dashboard_events"))
    assert response.status_code == 200
    assert response.json()["count"] == 6


def test_dashboard_calendar_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    url = reverse("api:dashboard_calendar")
    params = {"year": str(timezone.now().year), "organization": str(dashboard_setup["orgs"]["owner"].id)}
    response = dashboard_client.get(url, params)
    assert response.status_code == 200
    assert [evt["name"] for evt in response.json()] == ["In Owned Org"]


def test_dashboard_calendar_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    response = dashboard_client.get(reverse("api:dashboard_calendar"), {"year": timezone.now().year})
    assert response.status_code == 200
    names = {evt["name"] for evt in response.json()}
    assert {"In Owned Org", "In Staff Org", "In Member Org"} <= names


# ---- /dashboard/event_series ----


def test_dashboard_event_series_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    owner_org = dashboard_setup["orgs"]["owner"]
    staff_org = dashboard_setup["orgs"]["staff"]
    EventSeries.objects.create(organization=owner_org, name="Owned Series", slug="owned-series")
    EventSeries.objects.create(organization=staff_org, name="Staff Series", slug="staff-series")
    url = reverse("api:dashboard_event_series")
    response = dashboard_client.get(url, {"organization": str(owner_org.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["name"] == "Owned Series"


def test_dashboard_event_series_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    owner_org = dashboard_setup["orgs"]["owner"]
    staff_org = dashboard_setup["orgs"]["staff"]
    EventSeries.objects.create(organization=owner_org, name="Owned Series", slug="owned-series")
    EventSeries.objects.create(organization=staff_org, name="Staff Series", slug="staff-series")
    response = dashboard_client.get(reverse("api:dashboard_event_series"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


# ---- /dashboard/organizations ----


def test_dashboard_organizations_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    url = reverse("api:dashboard_organizations")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["staff"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["name"] == "Staff Org"


def test_dashboard_organizations_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    response = dashboard_client.get(reverse("api:dashboard_organizations"))
    assert response.status_code == 200
    assert response.json()["count"] == 3


# ---- /me/memberships, /me/membership-subscriptions, /me/applications ----


def test_list_my_memberships_organization_filter_narrows(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    second = Organization.objects.create(name="Second Member Org", owner=RevelUser.objects.create_user("secondowner"))
    OrganizationMember.objects.create(organization=second, user=dashboard_setup["user"])
    url = reverse("api:list_my_memberships")
    response = dashboard_client.get(url, {"organization": str(dashboard_setup["orgs"]["member"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["organization_id"] == str(dashboard_setup["orgs"]["member"].id)


def test_list_my_memberships_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_setup: dict[str, t.Any]
) -> None:
    second = Organization.objects.create(name="Second Member Org", owner=RevelUser.objects.create_user("secondowner"))
    OrganizationMember.objects.create(organization=second, user=dashboard_setup["user"])
    response = dashboard_client.get(reverse("api:list_my_memberships"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


def _subscribe(user: RevelUser, org_name: str) -> Organization:
    """Create an organization and subscribe ``user`` to its default membership tier."""
    org = Organization.objects.create(name=org_name, owner=RevelUser.objects.create_user(f"{org_name}-owner"))
    tier = MembershipTier.objects.get(organization=org, name="General membership")
    plan = subscription_plans.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )
    subscription_lifecycle.create_subscription(plan, user)
    return org


def test_list_my_subscriptions_organization_filter_narrows(dashboard_client: Client, dashboard_user: RevelUser) -> None:
    org_a = _subscribe(dashboard_user, "Sub Org A")
    _subscribe(dashboard_user, "Sub Org B")
    url = reverse("api:list_my_membership_subscriptions")
    response = dashboard_client.get(url, {"organization": str(org_a.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["organization_name"] == "Sub Org A"


def test_list_my_subscriptions_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_user: RevelUser
) -> None:
    _subscribe(dashboard_user, "Sub Org A")
    _subscribe(dashboard_user, "Sub Org B")
    response = dashboard_client.get(reverse("api:list_my_membership_subscriptions"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_list_my_applications_organization_filter_narrows(dashboard_client: Client, dashboard_user: RevelUser) -> None:
    org_a = Organization.objects.create(name="App Org A", owner=RevelUser.objects.create_user("app-owner-a"))
    org_b = Organization.objects.create(name="App Org B", owner=RevelUser.objects.create_user("app-owner-b"))
    OrganizationMembershipRequest.objects.create(organization=org_a, user=dashboard_user)
    OrganizationMembershipRequest.objects.create(organization=org_b, user=dashboard_user)
    url = reverse("api:list_membership_applications")
    response = dashboard_client.get(url, {"organization": str(org_a.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["organization_id"] == str(org_a.id)


def test_list_my_applications_no_organization_filter_returns_all(
    dashboard_client: Client, dashboard_user: RevelUser
) -> None:
    org_a = Organization.objects.create(name="App Org A", owner=RevelUser.objects.create_user("app-owner-a"))
    org_b = Organization.objects.create(name="App Org B", owner=RevelUser.objects.create_user("app-owner-b"))
    OrganizationMembershipRequest.objects.create(organization=org_a, user=dashboard_user)
    OrganizationMembershipRequest.objects.create(organization=org_b, user=dashboard_user)
    response = dashboard_client.get(reverse("api:list_membership_applications"))
    assert response.status_code == 200
    assert response.json()["count"] == 2
