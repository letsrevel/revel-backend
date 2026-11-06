"""Tests for private organizer notes about a person (#1314): CRUD, permissions, list exposure."""

import typing as t
import uuid

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Organization, OrganizationMember, OrganizationStaff, OrganizationUserNote
from events.service import organization_service

pytestmark = pytest.mark.django_db


def _note_url(organization: Organization, user: RevelUser | uuid.UUID, name: str = "get_user_note") -> str:
    user_id = user.id if isinstance(user, RevelUser) else user
    return reverse(f"api:{name}", kwargs={"slug": organization.slug, "user_id": user_id})


def _put(client: Client, organization: Organization, user: RevelUser, body: str) -> t.Any:
    return client.put(
        _note_url(organization, user, "upsert_user_note"), data={"body": body}, content_type="application/json"
    )


class TestNoteCrud:
    def test_get_404_when_none(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        assert organization_owner_client.get(_note_url(organization, member_user)).status_code == 404

    def test_put_creates_then_updates(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        first = _put(organization_owner_client, organization, member_user, "Allergic to **nuts**")
        assert first.status_code == 200, first.content
        assert first.json()["body"] == "Allergic to **nuts**"
        assert first.json()["updated_by_id"] == str(organization_owner_user.id)

        second = _put(organization_owner_client, organization, member_user, "Updated")
        assert second.status_code == 200
        assert OrganizationUserNote.objects.filter(organization=organization, user=member_user).count() == 1

        got = organization_owner_client.get(_note_url(organization, member_user))
        assert got.status_code == 200
        assert got.json()["body"] == "Updated"

    def test_put_unknown_user_404(self, organization_owner_client: Client, organization: Organization) -> None:
        response = organization_owner_client.put(
            _note_url(organization, uuid.uuid4(), "upsert_user_note"),
            data={"body": "x"},
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_delete(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="x")
        response = organization_owner_client.delete(_note_url(organization, member_user, "delete_user_note"))
        assert response.status_code == 204
        assert not OrganizationUserNote.objects.filter(organization=organization, user=member_user).exists()
        assert (
            organization_owner_client.delete(_note_url(organization, member_user, "delete_user_note")).status_code
            == 404
        )

    def test_notes_are_per_organization(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        other_org = Organization.objects.create(name="Other", slug="other", owner=revel_user_factory())
        OrganizationUserNote.objects.create(organization=other_org, user=member_user, body="secret of other org")
        assert organization_owner_client.get(_note_url(organization, member_user)).status_code == 404


class TestSanitizationAndLimits:
    def test_script_and_onclick_stripped(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        body = 'hello <script>alert(1)</script><a href="https://example.com" onclick="evil()">link</a>'
        response = _put(organization_owner_client, organization, member_user, body)
        assert response.status_code == 200, response.content
        stored = OrganizationUserNote.objects.get(organization=organization, user=member_user).body
        assert "<script" not in stored
        assert "alert(1)" not in stored
        assert "onclick" not in stored
        assert "hello" in stored

    def test_max_length_accepted(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, member_user, "a" * 5000).status_code == 200

    def test_over_max_length_rejected(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, member_user, "a" * 5001).status_code == 422
        assert not OrganizationUserNote.objects.exists()

    def test_empty_body_rejected(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        assert _put(organization_owner_client, organization, member_user, "").status_code == 422


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
        self, manager_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        assert _put(manager_client, organization, member_user, "ok").status_code == 200
        assert manager_client.get(_note_url(organization, member_user)).status_code == 200
        assert manager_client.delete(_note_url(organization, member_user, "delete_user_note")).status_code == 204

    def test_staff_without_manage_members_forbidden(
        self,
        organization_staff_client: Client,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Default staff has check_in_attendees but not manage_members: no API access to notes."""
        OrganizationUserNote.objects.create(organization=organization, user=member_user, body="x")
        assert organization_staff_client.get(_note_url(organization, member_user)).status_code == 403
        assert _put(organization_staff_client, organization, member_user, "y").status_code == 403
        assert (
            organization_staff_client.delete(_note_url(organization, member_user, "delete_user_note")).status_code
            == 403
        )

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
