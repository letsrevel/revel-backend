"""Tests for private organizer notes about a person (#1314): CRUD, permissions, list exposure."""

import typing as t
import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationUserNote,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.service import organization_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def target(member_user: RevelUser, organization: Organization) -> RevelUser:
    """A user the organization has a relationship with (a member), so notes may be written about them."""
    OrganizationMember.objects.create(organization=organization, user=member_user)
    return member_user


def _note_url(organization: Organization, user: RevelUser | uuid.UUID, name: str = "get_user_note") -> str:
    user_id = user.id if isinstance(user, RevelUser) else user
    return reverse(f"api:{name}", kwargs={"slug": organization.slug, "user_id": user_id})


def _put(client: Client, organization: Organization, user: RevelUser, body: str) -> t.Any:
    return client.put(
        _note_url(organization, user, "upsert_user_note"), data={"body": body}, content_type="application/json"
    )


class TestNoteCrud:
    def test_get_404_when_none(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert organization_owner_client.get(_note_url(organization, target)).status_code == 404

    def test_put_creates_then_updates(
        self,
        organization_owner_client: Client,
        organization: Organization,
        target: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        first = _put(organization_owner_client, organization, target, "Allergic to **nuts**")
        assert first.status_code == 200, first.content
        assert first.json()["body"] == "Allergic to **nuts**"
        assert first.json()["updated_by_id"] == str(organization_owner_user.id)

        second = _put(organization_owner_client, organization, target, "Updated")
        assert second.status_code == 200
        assert OrganizationUserNote.objects.filter(organization=organization, user=target).count() == 1

        got = organization_owner_client.get(_note_url(organization, target))
        assert got.status_code == 200
        assert got.json()["body"] == "Updated"

    def test_put_unknown_user_404(self, organization_owner_client: Client, organization: Organization) -> None:
        response = organization_owner_client.put(
            _note_url(organization, uuid.uuid4(), "upsert_user_note"),
            data={"body": "x"},
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_delete(self, organization_owner_client: Client, organization: Organization, target: RevelUser) -> None:
        OrganizationUserNote.objects.create(organization=organization, user=target, body="x")
        response = organization_owner_client.delete(_note_url(organization, target, "delete_user_note"))
        assert response.status_code == 204
        assert not OrganizationUserNote.objects.filter(organization=organization, user=target).exists()
        assert organization_owner_client.delete(_note_url(organization, target, "delete_user_note")).status_code == 404

    def test_notes_are_per_organization(
        self,
        organization_owner_client: Client,
        organization: Organization,
        target: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        other_org = Organization.objects.create(name="Other", slug="other", owner=revel_user_factory())
        OrganizationUserNote.objects.create(organization=other_org, user=target, body="secret of other org")
        assert organization_owner_client.get(_note_url(organization, target)).status_code == 404


class TestSanitizationAndLimits:
    def test_script_and_onclick_stripped(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        body = 'hello <script>alert(1)</script><a href="https://example.com" onclick="evil()">link</a>'
        response = _put(organization_owner_client, organization, target, body)
        assert response.status_code == 200, response.content
        stored = OrganizationUserNote.objects.get(organization=organization, user=target).body
        assert "<script" not in stored
        assert "alert(1)" not in stored
        assert "onclick" not in stored
        assert "hello" in stored

    def test_max_length_accepted(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, target, "a" * 5000).status_code == 200

    def test_over_max_length_rejected(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, target, "a" * 5001).status_code == 422
        assert not OrganizationUserNote.objects.exists()

    def test_empty_body_rejected(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, target, "").status_code == 422


class TestSanitizeBeforeValidate:
    def test_only_script_is_400(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, target, "<script>x</script>").status_code == 400
        assert not OrganizationUserNote.objects.exists()

    def test_whitespace_only_is_400(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, target, "   \n\t ").status_code == 400

    def test_ampersands_at_cap_accepted_and_reeditable(
        self, organization_owner_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        """Storage-side escaping (``&`` -> ``&amp;``) must not trip validation on later edits."""
        assert _put(organization_owner_client, organization, target, "&" * 5000).status_code == 200
        note = OrganizationUserNote.objects.get(organization=organization, user=target)
        assert len(note.body) > 5000
        assert _put(organization_owner_client, organization, target, "&" * 5000).status_code == 200
        note.refresh_from_db()
        note.save()  # full_clean on a stored (escaped) body stays valid


class TestRelationshipRestriction:
    def test_no_relationship_is_404_same_as_unknown_user(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        stranger = _put(organization_owner_client, organization, member_user, "x")
        unknown = organization_owner_client.put(
            _note_url(organization, uuid.uuid4(), "upsert_user_note"),
            data={"body": "x"},
            content_type="application/json",
        )
        assert stranger.status_code == unknown.status_code == 404
        assert stranger.json() == unknown.json()
        assert not OrganizationUserNote.objects.exists()

    def test_ticket_holder_allowed(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        nonmember_user: RevelUser,
    ) -> None:
        Ticket.objects.create(event=event, user=nonmember_user, tier=event_ticket_tier, guest_name="G")
        assert _put(organization_owner_client, organization, nonmember_user, "ok").status_code == 200

    def test_ticket_for_other_org_not_enough(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        other = Organization.objects.create(name="Elsewhere", slug="elsewhere", owner=organization_owner_user)
        OrganizationMember.objects.create(organization=other, user=nonmember_user)
        assert _put(organization_owner_client, organization, nonmember_user, "x").status_code == 404

    def test_series_pass_holder_allowed(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event_series: EventSeries,
        nonmember_user: RevelUser,
    ) -> None:
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Season",
            pro_rata_discount=Decimal("0"),
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=nonmember_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=series_pass.price,
        )
        assert _put(organization_owner_client, organization, nonmember_user, "ok").status_code == 200

    def test_removed_member_with_existing_note_can_still_be_read(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="kept")
        assert organization_owner_client.get(_note_url(organization, member_user)).status_code == 200


class TestPermissions:
    @pytest.fixture
    def manager_client(
        self, organization: Organization, organization_staff_user: RevelUser, organization_staff_client: Client
    ) -> Client:
        """Staff with manage_members."""
        staff = OrganizationStaff.objects.get(organization=organization, user=organization_staff_user)
        staff.permissions["default"]["manage_members"] = True
        staff.save(update_fields=["permissions"])
        return organization_staff_client

    def test_staff_with_manage_members_can_read_and_write(
        self, manager_client: Client, organization: Organization, target: RevelUser
    ) -> None:
        assert _put(manager_client, organization, target, "ok").status_code == 200
        assert manager_client.get(_note_url(organization, target)).status_code == 200
        assert manager_client.delete(_note_url(organization, target, "delete_user_note")).status_code == 204

    def test_staff_without_manage_members_forbidden(
        self,
        organization_staff_client: Client,
        organization: Organization,
        target: RevelUser,
    ) -> None:
        """Default staff has check_in_attendees but not manage_members: no API access to notes."""
        OrganizationUserNote.objects.create(organization=organization, user=target, body="x")
        assert organization_staff_client.get(_note_url(organization, target)).status_code == 403
        assert _put(organization_staff_client, organization, target, "y").status_code == 403
        assert organization_staff_client.delete(_note_url(organization, target, "delete_user_note")).status_code == 403

    def test_member_and_nonmember_denied(
        self,
        member_client: Client,
        nonmember_client: Client,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="x")
        for client in (member_client, nonmember_client):
            assert client.get(_note_url(organization, member_user)).status_code in (403, 404)
            assert _put(client, organization, member_user, "y").status_code in (403, 404)
            assert client.delete(_note_url(organization, member_user, "delete_user_note")).status_code in (403, 404)
        assert OrganizationUserNote.objects.get(organization=organization).body == "x"

    def test_anonymous_denied(self, client: Client, organization: Organization, member_user: RevelUser) -> None:
        assert client.get(_note_url(organization, member_user)).status_code == 401


class TestMemberListExposure:
    def _list(self, client: Client, organization: Organization) -> t.Any:
        return client.get(reverse("api:list_organization_members", kwargs={"slug": organization.slug}))

    def test_private_note_in_admin_list_and_survives_remove_readd(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user)
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="VIP")
        rows = self._list(organization_owner_client, organization).json()["results"]
        assert rows[0]["private_note"] == "VIP"

        organization_service.remove_member(organization, member_user)
        assert OrganizationUserNote.objects.filter(organization=organization, user=member_user).exists()
        assert self._list(organization_owner_client, organization).json()["results"] == []

        organization_service.add_member(organization, member_user, member.tier)  # type: ignore[arg-type]
        rows = self._list(organization_owner_client, organization).json()["results"]
        assert rows[0]["private_note"] == "VIP"

    def test_private_note_null_without_note(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user)
        assert self._list(organization_owner_client, organization).json()["results"][0]["private_note"] is None

    def test_staff_without_manage_members_never_sees_note_in_list(
        self,
        organization_staff_client: Client,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user)
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="hidden")
        response = self._list(organization_staff_client, organization)
        assert response.status_code == 200
        assert response.json()["results"][0]["private_note"] is None
        assert "hidden" not in response.content.decode()

    def test_no_n_plus_one_on_list(
        self,
        organization_owner_client: Client,
        organization: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        def seed(n: int) -> None:
            for _ in range(n):
                user = revel_user_factory()
                OrganizationMember.objects.create(organization=organization, user=user)
                OrganizationUserNote.objects.create(organization=organization, user=user, body="n")

        seed(2)
        self._list(organization_owner_client, organization)  # warm caches (content types, etc.)
        with CaptureQueriesContext(connection) as small:
            self._list(organization_owner_client, organization)
        seed(8)
        with CaptureQueriesContext(connection) as large:
            response = self._list(organization_owner_client, organization)
        assert len(response.json()["results"]) == 10
        assert len(large) == len(small)
