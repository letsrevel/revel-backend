"""``GET .../tickets/{ticket_id}`` carries the private note for the door scanner's confirm dialog."""

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    OrganizationStaff,
    OrganizationUserNote,
    PermissionMap,
    PermissionsSchema,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db

NOTE = "Prefers quiet seating"


@pytest.fixture
def ticket(event: Event, offline_tier: TicketTier, public_user: RevelUser) -> Ticket:
    return Ticket.objects.create(
        guest_name="Guest", user=public_user, event=event, tier=offline_tier, status=Ticket.TicketStatus.ACTIVE
    )


def _detail(client: Client, event: Event, ticket: Ticket) -> dict[str, object]:
    response = client.get(reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk}))
    assert response.status_code == 200, response.content
    data: dict[str, object] = response.json()
    return data


def _noted(event: Event, ticket: Ticket) -> None:
    OrganizationUserNote.objects.create(organization=event.organization, user=ticket.user, body=NOTE)


def _staff_client(event: Event, user: RevelUser, **permissions: bool) -> Client:
    OrganizationStaff.objects.create(
        organization=event.organization,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(**permissions)).model_dump(mode="json"),
    )
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def test_owner_sees_note(organization_owner_client: Client, event: Event, ticket: Ticket) -> None:
    _noted(event, ticket)
    assert _detail(organization_owner_client, event, ticket)["private_note"] == NOTE


def test_check_in_staff_sees_note(event: Event, ticket: Ticket, organization_staff_user: RevelUser) -> None:
    _noted(event, ticket)
    client = _staff_client(event, organization_staff_user, check_in_attendees=True)
    assert _detail(client, event, ticket)["private_note"] == NOTE


def test_staff_without_check_in_permission_sees_null(
    event: Event, ticket: Ticket, organization_staff_user: RevelUser
) -> None:
    _noted(event, ticket)
    client = _staff_client(event, organization_staff_user, manage_tickets=True, check_in_attendees=False)
    data = _detail(client, event, ticket)
    assert data["private_note"] is None
    assert NOTE not in str(data)


def test_null_without_note(organization_owner_client: Client, event: Event, ticket: Ticket) -> None:
    assert _detail(organization_owner_client, event, ticket)["private_note"] is None


def test_other_orgs_note_not_leaked(
    organization_owner_client: Client, event: Event, ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    from events.models import Organization

    other = Organization.objects.create(name="Elsewhere", slug="elsewhere", owner=organization_owner_user)
    OrganizationUserNote.objects.create(organization=other, user=ticket.user, body="other org's note")
    assert _detail(organization_owner_client, event, ticket)["private_note"] is None


def test_list_has_no_private_note(organization_owner_client: Client, event: Event, ticket: Ticket) -> None:
    _noted(event, ticket)
    response = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    assert response.status_code == 200
    rows = response.json()["results"]
    assert rows
    assert all("private_note" not in row for row in rows)
    assert NOTE not in response.content.decode()


def test_note_adds_no_queries(organization_owner_client: Client, event: Event, ticket: Ticket) -> None:
    """The note is a subquery on the ticket fetch: with or without one, the query count is the same."""
    url = reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    with CaptureQueriesContext(connection) as without:
        assert organization_owner_client.get(url).status_code == 200
    _noted(event, ticket)
    with CaptureQueriesContext(connection) as with_note:
        response = organization_owner_client.get(url)
    assert response.json()["private_note"] == NOTE
    assert len(with_note) == len(without)


def test_staff_permission_checks_cost_two_queries(
    organization_owner_client: Client, event: Event, ticket: Ticket, organization_staff_user: RevelUser
) -> None:
    """Non-owner staff pay two staff-row reads over the owner (who short-circuits both): the route
    permission and the ``check_in_attendees`` gate for the note. The note itself is a subquery."""
    url = reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    _noted(event, ticket)
    staff = _staff_client(event, organization_staff_user, check_in_attendees=True)
    with CaptureQueriesContext(connection) as owner_queries:
        assert organization_owner_client.get(url).status_code == 200
    with CaptureQueriesContext(connection) as staff_queries:
        assert staff.get(url).status_code == 200
    assert len(staff_queries) - len(owner_queries) == 2
