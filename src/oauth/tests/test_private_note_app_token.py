"""Private organizer notes (#1314) are never readable through OAuth app tokens, only sessions."""

import datetime as dt
from decimal import Decimal

import pytest
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationMember, OrganizationUserNote, Ticket, TicketTier
from oauth.models import OAuthApplication
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db

NOTE = "Prefers the quiet room"


def _app_client(user: RevelUser, app: OAuthApplication, scopes: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {make_access_token(user, app, scopes)}")


@pytest.fixture
def noted_member(organization: Organization, oauth_org_member: OrganizationMember) -> OrganizationMember:
    OrganizationUserNote.objects.create(organization=organization, user=oauth_org_member.user, body=NOTE)
    return oauth_org_member


@pytest.fixture
def open_event(oauth_event: Event) -> Event:
    now = timezone.now()
    oauth_event.status = Event.EventStatus.OPEN
    oauth_event.check_in_starts_at = now - dt.timedelta(hours=1)
    oauth_event.check_in_ends_at = now + dt.timedelta(hours=1)
    oauth_event.save(update_fields=["status", "check_in_starts_at", "check_in_ends_at"])
    return oauth_event


def test_members_list(
    organization: Organization,
    noted_member: OrganizationMember,
    oauth_app: OAuthApplication,
    session_client: Client,
) -> None:
    path = f"/api/organization-admin/{organization.slug}/members"
    app = _app_client(organization.owner, oauth_app, "org:read org:members").get(path)
    assert app.status_code == 200, app.content
    assert app.json()["results"][0]["private_note"] is None
    assert NOTE not in app.content.decode()
    session = session_client.get(path)
    assert session.json()["results"][0]["private_note"] == NOTE


def test_member_update_and_add(
    organization: Organization,
    noted_member: OrganizationMember,
    oauth_app: OAuthApplication,
    session_client: Client,
) -> None:
    path = f"/api/organization-admin/{organization.slug}/members/{noted_member.user_id}"
    app = _app_client(organization.owner, oauth_app, "org:read org:members").put(
        path, data={"status": "active"}, content_type="application/json"
    )
    assert app.status_code == 200, app.content
    assert app.json()["private_note"] is None
    session = session_client.put(path, data={"status": "active"}, content_type="application/json")
    assert session.json()["private_note"] == NOTE
    empty = session_client.put(path, data={}, content_type="application/json")
    assert empty.json()["private_note"] == NOTE
    app_empty = _app_client(organization.owner, oauth_app, "org:read org:members").put(
        path, data={}, content_type="application/json"
    )
    assert app_empty.json()["private_note"] is None


def test_verify(
    organization: Organization,
    noted_member: OrganizationMember,
    oauth_app: OAuthApplication,
    session_client: Client,
) -> None:
    path = f"/api/organization-admin/{organization.slug}/members/verify/{noted_member.qr_payload}"
    app = _app_client(organization.owner, oauth_app, "org:read org:checkin").get(path)
    assert app.status_code == 200, app.content
    assert app.json()["private_note"] is None
    assert session_client.get(path).json()["private_note"] == NOTE


def test_member_card_scan_no_ticket(
    open_event: Event,
    noted_member: OrganizationMember,
    organization: Organization,
    oauth_app: OAuthApplication,
    session_client: Client,
) -> None:
    path = f"/api/event-admin/{open_event.id}/tickets/{noted_member.qr_payload}/check-in"
    app = _app_client(organization.owner, oauth_app, "org:read org:checkin").post(path, content_type="application/json")
    assert app.status_code == 200, app.content
    assert app.json()["member"]["private_note"] is None
    session = session_client.post(path, content_type="application/json")
    assert session.json()["member"]["private_note"] == NOTE


@pytest.mark.parametrize("via_member_card", [False, True], ids=["ticket_code", "member_card_single_ticket"])
def test_ticket_scan(
    via_member_card: bool,
    open_event: Event,
    noted_member: OrganizationMember,
    organization: Organization,
    oauth_app: OAuthApplication,
) -> None:
    tier = TicketTier.objects.create(
        event=open_event, name="Free", price=Decimal("0"), payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket = Ticket.objects.create(
        event=open_event, user=noted_member.user, tier=tier, guest_name="G", status=Ticket.TicketStatus.ACTIVE
    )
    code = noted_member.qr_payload if via_member_card else str(ticket.id)
    path = f"/api/event-admin/{open_event.id}/tickets/{code}/check-in"
    app = _app_client(organization.owner, oauth_app, "org:read org:checkin").post(path, content_type="application/json")
    assert app.status_code == 200, app.content
    assert app.json()["kind"] == "checked_in"
    assert app.json()["private_note"] is None


@pytest.mark.parametrize("via_member_card", [False, True], ids=["ticket_code", "member_card_single_ticket"])
def test_ticket_scan_session_gets_note(
    via_member_card: bool, open_event: Event, noted_member: OrganizationMember, session_client: Client
) -> None:
    tier = TicketTier.objects.create(
        event=open_event, name="Free", price=Decimal("0"), payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket = Ticket.objects.create(
        event=open_event, user=noted_member.user, tier=tier, guest_name="G", status=Ticket.TicketStatus.ACTIVE
    )
    code = noted_member.qr_payload if via_member_card else str(ticket.id)
    path = f"/api/event-admin/{open_event.id}/tickets/{code}/check-in"
    assert session_client.post(path, content_type="application/json").json()["private_note"] == NOTE


def test_notes_endpoints_refuse_app_tokens(
    organization: Organization, noted_member: OrganizationMember, oauth_app: OAuthApplication
) -> None:
    path = f"/api/organization-admin/{organization.slug}/users/{noted_member.user_id}/note"
    client = _app_client(organization.owner, oauth_app, "org:read org:members org:checkin")
    for response in (
        client.get(path),
        client.put(path, data={"body": "x"}, content_type="application/json"),
        client.delete(path),
    ):
        assert response.status_code == 401, response.content
    assert OrganizationUserNote.objects.get(user=noted_member.user).body == NOTE
