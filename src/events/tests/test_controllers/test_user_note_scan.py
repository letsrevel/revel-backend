"""Private organizer notes on scan surfaces, schema pinning and GDPR handling (#1314)."""

import io
import typing as t
import zipfile
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from accounts.service import gdpr
from api.api import api
from conftest import RevelUserFactory
from events.models import (
    Event,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationUserNote,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


@pytest.fixture
def member(organization_membership: OrganizationMember) -> OrganizationMember:
    organization_membership.refresh_from_db()
    return organization_membership


def _check_in(client: Client, event: Event, code: str) -> t.Any:
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": code})
    return client.post(url, content_type="application/json")


def _note(organization: Organization, user: RevelUser, body: str = "Prefers quiet seating") -> OrganizationUserNote:
    return OrganizationUserNote.objects.create(organization=organization, user=user, body=body)


class TestTicketScan:
    def test_includes_note(
        self, organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, nonmember_user: RevelUser
    ) -> None:
        """A non-member attendee's ticket scan carries the note (notes are not tied to membership)."""
        ticket = Ticket.objects.create(event=event, user=nonmember_user, tier=event_ticket_tier, guest_name="G")
        _note(event.organization, nonmember_user)
        response = _check_in(organization_owner_client, event, str(ticket.id))
        assert response.status_code == 200, response.content
        assert response.json()["private_note"] == "Prefers quiet seating"

    def test_null_without_note(
        self, organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, nonmember_user: RevelUser
    ) -> None:
        ticket = Ticket.objects.create(event=event, user=nonmember_user, tier=event_ticket_tier, guest_name="G")
        response = _check_in(organization_owner_client, event, str(ticket.id))
        assert response.json()["private_note"] is None

    def test_other_orgs_note_not_leaked(
        self,
        organization_owner_client: Client,
        event: Event,
        event_ticket_tier: TicketTier,
        nonmember_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        other = Organization.objects.create(name="Elsewhere", slug="elsewhere", owner=organization_owner_user)
        _note(other, nonmember_user, "other org's note")
        ticket = Ticket.objects.create(event=event, user=nonmember_user, tier=event_ticket_tier, guest_name="G")
        assert _check_in(organization_owner_client, event, str(ticket.id)).json()["private_note"] is None

    def test_door_staff_without_manage_members_sees_note_read_only(
        self,
        organization_staff_client: Client,
        staff_member: OrganizationStaff,
        event: Event,
        event_ticket_tier: TicketTier,
        nonmember_user: RevelUser,
    ) -> None:
        ticket = Ticket.objects.create(event=event, user=nonmember_user, tier=event_ticket_tier, guest_name="G")
        _note(event.organization, nonmember_user)
        response = _check_in(organization_staff_client, event, str(ticket.id))
        assert response.status_code == 200, response.content
        assert response.json()["private_note"] == "Prefers quiet seating"


class TestMemberCardScan:
    def test_single_ticket_checked_in_includes_note(
        self,
        organization_owner_client: Client,
        event: Event,
        member: OrganizationMember,
        event_ticket_tier: TicketTier,
    ) -> None:
        Ticket.objects.create(event=event, user=member.user, tier=event_ticket_tier, guest_name="M")
        _note(event.organization, member.user)
        data = _check_in(organization_owner_client, event, member.qr_payload).json()
        assert data["kind"] == "checked_in"
        assert data["private_note"] == "Prefers quiet seating"

    def test_no_ticket_outcome_includes_note(
        self, organization_owner_client: Client, event: Event, member: OrganizationMember
    ) -> None:
        _note(event.organization, member.user)
        data = _check_in(organization_owner_client, event, member.qr_payload).json()
        assert data["kind"] == "member"
        assert data["member"]["private_note"] == "Prefers quiet seating"

    def test_no_ticket_outcome_null_without_note(
        self, organization_owner_client: Client, event: Event, member: OrganizationMember
    ) -> None:
        data = _check_in(organization_owner_client, event, member.qr_payload).json()
        assert data["member"]["private_note"] is None

    def test_multiple_tickets_outcome_includes_note(
        self,
        organization_owner_client: Client,
        event: Event,
        member: OrganizationMember,
        event_ticket_tier: TicketTier,
    ) -> None:
        for name in ("A", "B"):
            Ticket.objects.create(event=event, user=member.user, tier=event_ticket_tier, guest_name=name)
        _note(event.organization, member.user)
        data = _check_in(organization_owner_client, event, member.qr_payload).json()
        assert data["kind"] == "member"
        assert data["member"]["private_note"] == "Prefers quiet seating"


class TestVerifyEndpoint:
    def _verify(self, client: Client, organization: Organization, member: OrganizationMember) -> t.Any:
        url = reverse("api:verify_organization_member", kwargs={"slug": organization.slug, "code": member.qr_payload})
        return client.get(url)

    def test_includes_note_for_door_staff(
        self,
        organization_staff_client: Client,
        staff_member: OrganizationStaff,
        organization: Organization,
        member: OrganizationMember,
    ) -> None:
        _note(organization, member.user)
        response = self._verify(organization_staff_client, organization, member)
        assert response.status_code == 200
        assert response.json()["private_note"] == "Prefers quiet seating"

    def test_null_without_note(
        self, organization_owner_client: Client, organization: Organization, member: OrganizationMember
    ) -> None:
        assert self._verify(organization_owner_client, organization, member).json()["private_note"] is None

    def test_member_cannot_verify(
        self, member_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        member = OrganizationMember.objects.get(organization=organization, user=member_user)
        _note(organization, member.user)
        response = self._verify(member_client, organization, member)
        assert response.status_code in (403, 404)
        assert "Prefers quiet seating" not in response.content.decode()


# --- Pinning: ``private_note`` can only ever be reached through staff operations ---

# Every operation allowed to expose a schema that carries ``private_note``. A new entry here must be
# a staff-only endpoint; member- or attendee-facing reuse of these schemas would leak the note.
PRIVATE_NOTE_STAFF_OPERATIONS = {
    ("post", "/api/event-admin/{event_id}/tickets/{code}/check-in"),
    ("get", "/api/event-admin/{event_id}/tickets/{ticket_id}"),
    ("get", "/api/organization-admin/{slug}/members/verify/{code}"),
    ("get", "/api/organization-admin/{slug}/members"),
    ("put", "/api/organization-admin/{slug}/members/{user_id}"),
    ("post", "/api/organization-admin/{slug}/members/{user_id}"),
    ("get", "/api/organization-admin/{slug}/users/{user_id}/note"),
    ("put", "/api/organization-admin/{slug}/users/{user_id}/note"),
    ("get", "/api/organization-admin/{slug}/member-register/{record_id}/note"),
    ("put", "/api/organization-admin/{slug}/member-register/{record_id}/note"),
}


def _refs(node: t.Any) -> t.Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value.rsplit("/", 1)[-1]
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def test_private_note_schemas_only_reachable_from_staff_operations() -> None:
    spec = api.get_openapi_schema()
    components = spec["components"]["schemas"]

    # ``UserNoteSchema`` is the note itself, so it is a carrier even though it has no ``private_note`` field.
    carriers = {name for name, comp in components.items() if "private_note" in comp.get("properties", {})}
    carriers.add("UserNoteSchema")
    assert len(carriers) > 1, "expected schemas carrying private_note"

    # Transitive closure: any component that (indirectly) embeds a carrier is also a carrier.
    changed = True
    while changed:
        changed = False
        for name, comp in components.items():
            if name not in carriers and set(_refs(comp)) & carriers:
                carriers.add(name)
                changed = True

    exposing: set[tuple[str, str]] = set()
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if isinstance(operation, dict) and set(_refs(operation)) & carriers:
                exposing.add((method, path))

    assert exposing == PRIVATE_NOTE_STAFF_OPERATIONS, exposing ^ PRIVATE_NOTE_STAFF_OPERATIONS


# --- GDPR ---


def test_note_not_in_self_service_export(
    organization: Organization, member_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    OrganizationUserNote.objects.create(
        organization=organization, user=member_user, body="UNIQUE-NOTE-MARKER", updated_by=organization_owner_user
    )
    for user in (member_user, organization_owner_user):
        export = gdpr.generate_user_data_export(user)
        with zipfile.ZipFile(io.BytesIO(export.file.read())) as zf:
            assert "UNIQUE-NOTE-MARKER" not in zf.read("revel_user_data.json").decode()


def test_erasure_removes_note_about_user_and_keeps_note_when_author_deleted(
    organization: Organization, member_user: RevelUser, revel_user_factory: RevelUserFactory
) -> None:
    revel_user = revel_user_factory()
    OrganizationUserNote.objects.create(organization=organization, user=member_user, body="about member")
    other = OrganizationUserNote.objects.create(
        organization=organization, user=revel_user, body="written by member", updated_by=member_user
    )
    member_user.delete()
    assert not OrganizationUserNote.objects.filter(body="about member").exists()
    other.refresh_from_db()
    assert other.updated_by is None
