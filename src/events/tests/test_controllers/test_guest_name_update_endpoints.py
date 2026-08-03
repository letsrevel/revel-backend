"""Owner and organizer guest_name PATCH endpoints (#845)."""

import json
import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, OrganizationStaff, Ticket

pytestmark = pytest.mark.django_db


def _owner_url(ticket: Ticket) -> str:
    return reverse("api:dashboard_update_ticket_guest_name", kwargs={"ticket_id": ticket.pk})


def _admin_url(event: Event, ticket: Ticket) -> str:
    return reverse("api:admin_update_ticket_guest_name", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})


def _patch(client: Client, url: str, guest_name: str) -> t.Any:
    return client.patch(url, data=json.dumps({"guest_name": guest_name}), content_type="application/json")


# ---- Owner (dashboard) ----


def test_owner_renames_own_ticket(member_client: Client, ticket: Ticket) -> None:
    """The ticket holder can rename their own ticket."""
    response = _patch(member_client, _owner_url(ticket), "Renamed Holder")

    assert response.status_code == 200, response.content
    assert response.json()["guest_name"] == "Renamed Holder"
    ticket.refresh_from_db()
    assert ticket.guest_name == "Renamed Holder"


def test_owner_cannot_rename_someone_elses_ticket(nonmember_client: Client, ticket: Ticket) -> None:
    """Another user's ticket is invisible on the dashboard route."""
    response = _patch(nonmember_client, _owner_url(ticket), "Renamed Holder")

    assert response.status_code == 404
    ticket.refresh_from_db()
    assert ticket.guest_name != "Renamed Holder"


def test_owner_route_requires_authentication(ticket: Ticket) -> None:
    """Anonymous callers get 401."""
    response = _patch(Client(), _owner_url(ticket), "Renamed Holder")

    assert response.status_code == 401


def test_owner_cannot_rename_checked_in_ticket(member_client: Client, ticket: Ticket) -> None:
    """Checked-in tickets are frozen (409)."""
    ticket.status = Ticket.TicketStatus.CHECKED_IN
    ticket.save(update_fields=["status"])

    response = _patch(member_client, _owner_url(ticket), "Renamed Holder")

    assert response.status_code == 409


def test_owner_cannot_clear_name_when_event_requires_it(member_client: Client, ticket: Ticket) -> None:
    """Clearing the name is refused with 400 when the event requires names."""
    assert ticket.event.require_ticket_names is True

    response = _patch(member_client, _owner_url(ticket), "")

    assert response.status_code == 400
    assert response.json()["detail"] == "This event requires a name on every ticket."


def test_owner_can_clear_name_when_event_does_not_require_it(member_client: Client, ticket: Ticket) -> None:
    """Clearing the name is allowed when the event doesn't require names."""
    ticket.event.require_ticket_names = False
    ticket.event.save(update_fields=["require_ticket_names"])

    response = _patch(member_client, _owner_url(ticket), "")

    assert response.status_code == 200, response.content
    assert response.json()["guest_name"] == ""


# ---- Organizer (event admin) ----


def test_organizer_renames_ticket(organization_owner_client: Client, event: Event, ticket: Ticket) -> None:
    """An organizer can rename an attendee's ticket."""
    response = _patch(organization_owner_client, _admin_url(event, ticket), "Organizer Rename")

    assert response.status_code == 200, response.content
    assert response.json()["guest_name"] == "Organizer Rename"
    ticket.refresh_from_db()
    assert ticket.guest_name == "Organizer Rename"


def test_organizer_route_rejects_staff_without_manage_tickets(
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
    event: Event,
    ticket: Ticket,
) -> None:
    """Staff without manage_tickets get 403."""
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    response = _patch(organization_staff_client, _admin_url(event, ticket), "Organizer Rename")

    assert response.status_code == 403


def test_organizer_route_404s_for_ticket_from_another_event(
    organization_owner_client: Client,
    event: Event,
    public_event: Event,
    ticket: Ticket,
) -> None:
    """A ticket that doesn't belong to the event in the path is a 404."""
    response = _patch(organization_owner_client, _admin_url(public_event, ticket), "Organizer Rename")

    assert response.status_code == 404


def test_organizer_cannot_rename_cancelled_ticket(
    organization_owner_client: Client, event: Event, ticket: Ticket
) -> None:
    """Cancelled tickets are frozen (409)."""
    ticket.status = Ticket.TicketStatus.CANCELLED
    ticket.save(update_fields=["status"])

    response = _patch(organization_owner_client, _admin_url(event, ticket), "Organizer Rename")

    assert response.status_code == 409
