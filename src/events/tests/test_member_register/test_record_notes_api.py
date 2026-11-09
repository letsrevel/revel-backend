"""API for staff notes on register entries: proxying to the user's note, permissions, has_note flag."""

import typing as t
from datetime import timedelta

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization, OrganizationMember, OrganizationUserNote
from events.models.user_note import USER_NOTE_MAX_LENGTH
from events.service.member_register_reconciliation import merge_records

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db


def _url(organization: Organization, record: MembershipRecord, name: str = "get_member_register_record_note") -> str:
    return reverse(f"api:{name}", kwargs={"slug": organization.slug, "record_id": record.pk})


def _put(client: Client, organization: Organization, record: MembershipRecord, body: str) -> t.Any:
    return client.put(
        _url(organization, record, "upsert_member_register_record_note"),
        data={"body": body},
        content_type="application/json",
    )


def _user_note_url(organization: Organization, user: RevelUser) -> str:
    return reverse("api:get_user_note", kwargs={"slug": organization.slug, "user_id": user.pk})


@pytest.fixture
def linked_closed(organization: Organization, claimant: RevelUser, make_record: RecordFactory) -> MembershipRecord:
    """A closed record linked to an account that has no other relationship with the organization."""
    return make_record(
        user=claimant, valid_from=timezone.now() - timedelta(days=9), valid_until=timezone.now() - timedelta(days=1)
    )


class TestUnlinkedRecord:
    def test_put_get_delete_use_the_record_note(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        organization_owner_user: RevelUser,
    ) -> None:
        record = make_record()
        assert owner_client.get(_url(organization, record)).status_code == 404
        put = _put(owner_client, organization, record, "Call **before** noon")
        assert put.status_code == 200, put.content
        assert put.json()["updated_by_id"] == str(organization_owner_user.pk)
        note = OrganizationUserNote.objects.get()
        assert (note.record_id, note.user_id) == (record.pk, None)
        assert owner_client.get(_url(organization, record)).json()["body"] == "Call **before** noon"
        assert _put(owner_client, organization, record, "Replaced").status_code == 200
        assert OrganizationUserNote.objects.get().body == "Replaced"
        assert owner_client.delete(_url(organization, record, "delete_member_register_record_note")).status_code == 204
        assert not OrganizationUserNote.objects.exists()
        assert owner_client.delete(_url(organization, record, "delete_member_register_record_note")).status_code == 404


class TestLinkedRecord:
    def test_operates_on_the_users_note(
        self, owner_client: Client, organization: Organization, linked_closed: MembershipRecord, claimant: RevelUser
    ) -> None:
        # The user has no other relationship than this closed record: the entry itself is the relationship.
        assert _put(owner_client, organization, linked_closed, "About them").status_code == 200
        note = OrganizationUserNote.objects.get()
        assert (note.user_id, note.record_id) == (claimant.pk, None)
        assert owner_client.get(_user_note_url(organization, claimant)).json()["body"] == "About them"
        assert owner_client.get(_url(organization, linked_closed)).json()["body"] == "About them"
        assert (
            owner_client.delete(_url(organization, linked_closed, "delete_member_register_record_note")).status_code
            == 204
        )
        assert not OrganizationUserNote.objects.exists()


class TestGuards:
    def test_merged_record_put_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        survivor = make_record(email="s@example.com")
        absorbed = make_record(email="a@example.com")
        merge_records(survivor, absorbed)
        response = _put(owner_client, organization, absorbed, "x")
        assert response.status_code == 400
        assert not OrganizationUserNote.objects.exists()

    def test_other_organization_record_is_404(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other", owner=organization.owner)
        foreign = make_record(organization=other)
        assert owner_client.get(_url(organization, foreign)).status_code == 404
        assert _put(owner_client, organization, foreign, "x").status_code == 404
        assert owner_client.delete(_url(organization, foreign, "delete_member_register_record_note")).status_code == 404

    def test_body_too_long_or_empty_is_400(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        record = make_record()
        assert _put(owner_client, organization, record, "a" * (USER_NOTE_MAX_LENGTH + 1)).status_code == 400
        assert _put(owner_client, organization, record, "   ").status_code in (400, 422)
        assert _put(owner_client, organization, record, "<script></script>").status_code == 400
        assert not OrganizationUserNote.objects.exists()


class TestPermissions:
    def test_matrix(
        self,
        owner_client: Client,
        staff_client: Client,
        outsider_client: Client,
        organization: Organization,
        make_record: RecordFactory,
    ) -> None:
        record = make_record()
        assert _put(owner_client, organization, record, "owner").status_code == 200
        assert _put(staff_client, organization, record, "staff").status_code == 200
        assert staff_client.get(_url(organization, record)).status_code == 200
        assert outsider_client.get(_url(organization, record)).status_code in (403, 404)

    def test_staff_without_manage_members_is_forbidden(
        self, plain_staff_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        record = make_record()
        assert plain_staff_client.get(_url(organization, record)).status_code == 403
        assert _put(plain_staff_client, organization, record, "no").status_code == 403
        deleted = plain_staff_client.delete(_url(organization, record, "delete_member_register_record_note"))
        assert deleted.status_code == 403

    def test_oauth_app_token_cannot_reach_it(
        self, organization: Organization, make_record: RecordFactory, organization_owner_user: RevelUser
    ) -> None:
        from oauth.models import OAuthApplication
        from oauth.scopes import SCOPES
        from oauth.tests.test_auth_class import make_access_token

        app = OAuthApplication.objects.create(
            user=organization_owner_user,
            name="App",
            client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
            redirect_uris="https://app.example/cb",
            allowed_scopes=sorted(SCOPES),
            registration_source=OAuthApplication.RegistrationSource.MANUAL,
        )
        record = make_record()
        OrganizationUserNote.objects.create(organization=organization, record=record, body="SECRET-NOTE")
        client = Client(
            HTTP_AUTHORIZATION=f"Bearer {make_access_token(organization_owner_user, app, ' '.join(SCOPES))}"
        )
        response = client.get(_url(organization, record))
        assert response.status_code in (401, 403)
        assert "SECRET-NOTE" not in response.content.decode()
        assert _put(client, organization, record, "x").status_code in (401, 403)


class TestHasNote:
    def test_flag_is_correct_and_query_count_is_flat(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        django_user_model: type[RevelUser],
    ) -> None:
        list_url = reverse("api:list_member_register", kwargs={"slug": organization.slug})

        def listing() -> tuple[dict[str, bool], int]:
            with CaptureQueriesContext(connection) as ctx:
                response = owner_client.get(list_url)
            assert response.status_code == 200, response.content
            return {r["full_name"]: r["has_note"] for r in response.json()["results"]}, len(ctx)

        make_record(full_name="unlinked-noted", email="a@example.com")
        _, baseline = listing()

        noted = MembershipRecord.objects.get(full_name="unlinked-noted")
        OrganizationUserNote.objects.create(organization=organization, record=noted, body="n")
        for i in range(4):
            user = django_user_model.objects.create_user(username=f"u{i}", email=f"u{i}@x.com", password="pw")
            make_record(
                full_name=f"linked-{i}",
                email=f"u{i}@x.com",
                user=user,
                valid_from=timezone.now() - timedelta(days=9),
                valid_until=timezone.now() - timedelta(days=1),
            )
            if i % 2 == 0:
                OrganizationUserNote.objects.create(organization=organization, user=user, body="n")
        make_record(full_name="unlinked-plain", email="b@example.com")

        flags, count = listing()
        assert count == baseline
        assert flags == {
            "unlinked-noted": True,
            "unlinked-plain": False,
            "linked-0": True,
            "linked-1": False,
            "linked-2": True,
            "linked-3": False,
        }

    def test_single_record_get(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        record = make_record()
        url = reverse("api:get_member_register_record", kwargs={"slug": organization.slug, "record_id": record.pk})
        assert owner_client.get(url).json()["has_note"] is False
        OrganizationUserNote.objects.create(organization=organization, record=record, body="n")
        assert owner_client.get(url).json()["has_note"] is True

    def test_user_note_does_not_flag_other_orgs_records(
        self, owner_client: Client, organization: Organization, claimant: RevelUser, make_record: RecordFactory
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other2", owner=organization.owner)
        OrganizationUserNote.objects.create(organization=other, user=claimant, body="n")
        record = make_record(user=claimant)
        OrganizationMember.objects.filter(organization=organization, user=claimant).delete()
        url = reverse("api:get_member_register_record", kwargs={"slug": organization.slug, "record_id": record.pk})
        assert owner_client.get(url).json()["has_note"] is False
