"""Review follow-ups for register-entry notes: truncation with markup, fallbacks, admin, list flags."""

import html
import typing as t
from contextlib import nullcontext
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.db import IntegrityError, connection
from django.test import RequestFactory
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from common.sanitizers import sanitize_markdown
from events.admin.membership_record import MembershipRecordAdmin
from events.models import (
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationUserNote,
)
from events.models.user_note import USER_NOTE_MAX_LENGTH
from events.service.member_register_reconciliation import merge_records
from events.service.user_note_transfer import transfer_record_note

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

STAMP = datetime(2026, 3, 2, 2, 30, tzinfo=dt_timezone.utc)
LATER = datetime(2026, 5, 1, 12, 0, tzinfo=dt_timezone.utc)


def _note(organization: Organization, body: str, **target: t.Any) -> OrganizationUserNote:
    return OrganizationUserNote.objects.create(organization=organization, body=body, **target)


def _stamp(note: OrganizationUserNote, when: datetime) -> OrganizationUserNote:
    OrganizationUserNote.objects.filter(pk=note.pk).update(updated_at=when)
    note.refresh_from_db()
    return note


class TestTruncationWithMarkup:
    @pytest.mark.parametrize(
        "unit",
        ["a & b ", "a < b ", "<strong>bold ", '<a href="http://example.com/some/long/path">link</a> ', "x &lt;b&gt; "],
    )
    def test_cap_marker_and_target_prefix(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser, unit: str
    ) -> None:
        target = _note(organization, "t" * 3000 + " &amp; <em>kept</em>", user=claimant)
        target_body = target.body
        record = make_record()
        _note(organization, unit * 1000, record=record)
        transfer_record_note(record, to_user_id=claimant.pk)
        stored = OrganizationUserNote.objects.get().body
        assert stored.startswith(target_body)
        assert "… [truncated]" in stored
        assert len(html.unescape(stored)) <= USER_NOTE_MAX_LENGTH
        assert len(html.unescape(sanitize_markdown(stored))) <= USER_NOTE_MAX_LENGTH
        assert not OrganizationUserNote.objects.filter(record__isnull=False).exists()

    def test_target_with_literal_entity_text_is_unchanged(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        target = _note(organization, "x &lt;b&gt; y", user=claimant)
        stored_before = target.body
        record = make_record()
        _note(organization, "from record", record=record)
        transfer_record_note(record, to_user_id=claimant.pk)
        body = OrganizationUserNote.objects.get().body
        assert body.startswith(stored_before) and body.endswith("from record")

    def test_dropped_keeps_target_author_and_stamp(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser, django_user_model: t.Any
    ) -> None:
        editor = django_user_model.objects.create_user(username="e1", email="e1@example.com", password="pw")
        other = django_user_model.objects.create_user(username="e2", email="e2@example.com", password="pw")
        target = _stamp(_note(organization, "u" * (USER_NOTE_MAX_LENGTH - 5), user=claimant, updated_by=editor), STAMP)
        record = make_record()
        _stamp(_note(organization, "record", record=record, updated_by=other), LATER)
        transfer_record_note(record, to_user_id=claimant.pk)
        target.refresh_from_db()
        assert (target.updated_by_id, target.updated_at) == (editor.pk, STAMP)
        assert OrganizationUserNote.objects.count() == 1


class TestFallback:
    def test_concurrent_target_creation_falls_back_to_merge(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        record = make_record()
        _note(organization, "from record", record=record)
        real_save = OrganizationUserNote.save

        def racing_save(self: OrganizationUserNote, *args: t.Any, **kwargs: t.Any) -> None:
            if self.record_id is None and self.user_id == claimant.pk and self.body == "from record":
                # Another request created the target note first (committed outside our savepoint).
                OrganizationUserNote.objects.bulk_create(
                    [OrganizationUserNote(organization=organization, user=claimant, body="theirs")]
                )
                raise IntegrityError("duplicate")
            real_save(self, *args, **kwargs)

        # No real savepoint: the competing note must survive the failed re-point, as if committed elsewhere.
        with (
            patch.object(OrganizationUserNote, "save", racing_save),
            patch("events.service.user_note_transfer.transaction.atomic", return_value=nullcontext()),
        ):
            transfer_record_note(record, to_user_id=claimant.pk)
        note = OrganizationUserNote.objects.get()
        assert note.user_id == claimant.pk and note.body.startswith("theirs") and note.body.endswith("from record")

    def test_unexpected_failure_never_raises(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        record = make_record()
        _note(organization, "from record", record=record)
        with patch.object(OrganizationUserNote, "save", side_effect=IntegrityError("boom")):
            transfer_record_note(record, to_user_id=claimant.pk)
        assert OrganizationUserNote.objects.get().record_id == record.pk


class TestAdminLink:
    def test_linking_in_admin_moves_the_note(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser, admin_user: RevelUser
    ) -> None:
        record = make_record()
        note = _stamp(_note(organization, "staff text", record=record), STAMP)
        record.user = claimant
        request = RequestFactory().post("/")
        request.user = admin_user
        MembershipRecordAdmin(MembershipRecord, admin.site).save_model(request, record, None, True)
        note.refresh_from_db()
        assert (note.user_id, note.record_id, note.body, note.updated_at) == (claimant.pk, None, "staff text", STAMP)


class TestMergeAndOAuth:
    def test_merge_into_linked_survivor_with_user_note(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser, link_tier: MembershipTier
    ) -> None:
        survivor = make_record(user=claimant, email=claimant.email, valid_from=timezone.now() - timedelta(days=5))
        OrganizationMember.objects.get_or_create(organization=organization, user=claimant, defaults={"tier": link_tier})
        user_note = _note(organization, "User text", user=claimant)
        absorbed = make_record(email="dup@example.com")
        _stamp(_note(organization, "Record text", record=absorbed), STAMP)
        merge_records(survivor, absorbed)
        user_note.refresh_from_db()
        assert user_note.body.startswith("User text\n\n— From register entry (")
        assert user_note.body.endswith("Record text")
        assert OrganizationUserNote.objects.count() == 1

    def test_oauth_app_token_cannot_delete(
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
        _note(organization, "keep me", record=record)
        token = make_access_token(organization_owner_user, app, " ".join(SCOPES))
        client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
        url = reverse(
            "api:delete_member_register_record_note", kwargs={"slug": organization.slug, "record_id": record.pk}
        )
        assert client.delete(url).status_code in (401, 403)
        assert OrganizationUserNote.objects.count() == 1


class TestListFlags:
    def test_possible_duplicates_flags_and_flat_queries(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory, django_user_model: t.Any
    ) -> None:
        url = reverse("api:list_member_register_possible_duplicates", kwargs={"slug": organization.slug})

        def run() -> tuple[list[dict[str, t.Any]], int]:
            with CaptureQueriesContext(connection) as ctx:
                response = owner_client.get(url)
            assert response.status_code == 200, response.content
            return response.json(), len(ctx)

        user = django_user_model.objects.create_user(username="l0", email="l0@x.com", password="pw")
        make_record(user=user, email="shared0@x.com", full_name="L0")
        make_record(email="shared0@x.com", full_name="U0")
        _, baseline = run()
        for i in range(1, 4):
            user = django_user_model.objects.create_user(username=f"l{i}", email=f"l{i}@x.com", password="pw")
            linked = make_record(user=user, email=f"shared{i}@x.com", full_name=f"L{i}")
            unlinked = make_record(email=f"shared{i}@x.com", full_name=f"U{i}")
            if i % 2:
                _note(organization, "n", user=user)
                _note(organization, "n", record=unlinked)
            assert linked
        pairs, count = run()
        assert count == baseline
        flags = {p["unlinked"]["full_name"]: (p["unlinked"]["has_note"], p["linked"]["has_note"]) for p in pairs}
        assert flags == {"U0": (False, False), "U1": (True, True), "U2": (False, False), "U3": (True, True)}

    def test_register_matches_flags_and_flat_queries(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        link_tier: MembershipTier,
    ) -> None:
        application = OrganizationMembershipRequest.objects.create(
            organization=organization, user=claimant, tier=link_tier
        )
        url = reverse(
            "api:list_membership_request_register_matches",
            kwargs={"slug": organization.slug, "request_id": application.pk},
        )

        def run() -> tuple[list[dict[str, t.Any]], int]:
            with CaptureQueriesContext(connection) as ctx:
                response = owner_client.get(url)
            assert response.status_code == 200, response.content
            return response.json(), len(ctx)

        make_record(email=claimant.email, full_name="M0")
        _, baseline = run()
        for i in range(1, 4):
            record = make_record(email=claimant.email, full_name=f"M{i}")
            if i % 2:
                _note(organization, "n", record=record)
        matches, count = run()
        assert count == baseline
        assert {m["record"]["full_name"]: m["record"]["has_note"] for m in matches} == {
            "M0": False,
            "M1": True,
            "M2": False,
            "M3": True,
        }
