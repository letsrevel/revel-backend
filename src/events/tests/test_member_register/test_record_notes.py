"""Staff notes on account-less register entries: model rules and the move onto the user when linked."""

import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationUserNote,
)
from events.models.user_note import USER_NOTE_MAX_LENGTH
from events.service import member_register_linking as linking
from events.service.member_register_reconciliation import merge_records, unlink_record
from events.service.organization_service import membership as membership_service
from events.service.user_note_transfer import transfer_record_note

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)
STAMP = datetime(2026, 3, 2, 2, 30, tzinfo=dt_timezone.utc)  # 2026-03-01 in New York
LATER = datetime(2026, 5, 1, 12, 0, tzinfo=dt_timezone.utc)


def _note(organization: Organization, body: str = "Record note", **target: t.Any) -> OrganizationUserNote:
    return OrganizationUserNote.objects.create(organization=organization, body=body, **target)


def _stamp(note: OrganizationUserNote, when: datetime) -> OrganizationUserNote:
    OrganizationUserNote.objects.filter(pk=note.pk).update(updated_at=when)
    note.refresh_from_db()
    return note


@pytest.fixture
def editor(django_user_model: type[RevelUser]) -> RevelUser:
    """Someone who wrote a note."""
    return django_user_model.objects.create_user(username="editor", email="editor@example.com", password="pw")


@pytest.fixture
def other_editor(django_user_model: type[RevelUser]) -> RevelUser:
    """Someone else who wrote a note."""
    return django_user_model.objects.create_user(username="editor2", email="editor2@example.com", password="pw")


@pytest.fixture
def account(django_user_model: type[RevelUser]) -> RevelUser:
    """A verified account whose email matches no register entry."""
    return django_user_model.objects.create_user(
        username="acct@example.com", email="acct@example.com", password="pw", email_verified=True
    )


@pytest.fixture
def linked_survivor(
    organization: Organization, account: RevelUser, link_tier: MembershipTier, make_record: RecordFactory
) -> MembershipRecord:
    """A linked open STANDARD record with its access row."""
    record = make_record(user=account, full_name="Survivor", email="acct@example.com", valid_from=_ago(5))
    OrganizationMember.objects.create(
        organization=organization, user=account, tier=link_tier, status=OrganizationMember.MembershipStatus.ACTIVE
    )
    return record


def _ago(days: int) -> datetime:
    return timezone.now() - days * DAY


class TestModel:
    def test_user_and_record_both_set_is_invalid(
        self, organization: Organization, account: RevelUser, make_record: RecordFactory
    ) -> None:
        with pytest.raises(ValidationError):
            _note(organization, user=account, record=make_record())

    def test_neither_set_is_invalid(self, organization: Organization) -> None:
        with pytest.raises(ValidationError):
            _note(organization)

    def test_second_note_for_a_record_is_invalid(self, organization: Organization, make_record: RecordFactory) -> None:
        record = make_record()
        _note(organization, record=record)
        with pytest.raises(ValidationError):
            _note(organization, record=record)

    def test_record_of_another_organization_is_invalid(
        self, organization: Organization, make_record: RecordFactory, django_user_model: type[RevelUser]
    ) -> None:
        other = Organization.objects.create(
            name="Other", slug="other", owner=django_user_model.objects.create_user(username="o", password="pw")
        )
        record = make_record()
        with pytest.raises(ValidationError):
            _note(other, record=record)

    def test_deleting_the_record_deletes_its_note(self, organization: Organization, make_record: RecordFactory) -> None:
        record = make_record()
        _note(organization, record=record)
        record.delete()
        assert not OrganizationUserNote.objects.exists()


class TestTransferOnLink:
    """Each link path re-points a note when the user has none: same row, same body/author/stamp."""

    def _assert_moved(self, note: OrganizationUserNote, user: RevelUser, editor: RevelUser) -> None:
        moved = OrganizationUserNote.objects.get(pk=note.pk)
        assert (moved.user_id, moved.record_id) == (user.pk, None)
        assert (moved.body, moved.updated_by_id, moved.updated_at) == ("Record note", editor.pk, STAMP)
        assert OrganizationUserNote.objects.count() == 1

    def _noted_record(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser, **extra: t.Any
    ) -> tuple[MembershipRecord, OrganizationUserNote]:
        record = make_record(**extra)
        note = _stamp(_note(organization, record=record, updated_by=editor), STAMP)
        return record, note

    def test_invite_claim(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser, claimant: RevelUser
    ) -> None:
        record, note = self._noted_record(organization, make_record, editor, invite_token="tok")
        result = linking.claim_invite("tok", claimant)
        assert result is not None and result.outcome == linking.LinkOutcome.LINKED
        self._assert_moved(note, claimant, editor)

    def test_auto_email(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser, claimant: RevelUser
    ) -> None:
        _, note = self._noted_record(organization, make_record, editor, email=claimant.email)
        results = linking.auto_link_records_for_user(claimant.pk)
        assert [r.outcome for r in results] == [linking.LinkOutcome.LINKED]
        self._assert_moved(note, claimant, editor)

    def test_closed_record_claim(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser, claimant: RevelUser
    ) -> None:
        _, note = self._noted_record(
            organization,
            make_record,
            editor,
            invite_token="tok",
            valid_until=_ago(1),
            valid_from=_ago(9),
        )
        result = linking.link_record(MembershipRecord.objects.get(invite_token="tok"), claimant, token="tok")
        assert result.outcome == linking.LinkOutcome.LINKED
        self._assert_moved(note, claimant, editor)

    def test_adoption(
        self,
        organization: Organization,
        make_record: RecordFactory,
        editor: RevelUser,
        claimant: RevelUser,
        link_tier: MembershipTier,
    ) -> None:
        _, note = self._noted_record(organization, make_record, editor, email=claimant.email)
        # Creating the access row makes the register mirror adopt the matching record (adopt_unlinked_record).
        OrganizationMember.objects.create(organization=organization, user=claimant, tier=link_tier)
        assert MembershipRecord.objects.get(email=claimant.email).user_id == claimant.pk
        self._assert_moved(note, claimant, editor)

    def test_approval(
        self,
        organization: Organization,
        make_record: RecordFactory,
        editor: RevelUser,
        claimant: RevelUser,
        link_tier: MembershipTier,
        organization_owner_user: RevelUser,
    ) -> None:
        record, note = self._noted_record(organization, make_record, editor, email="nomatch@example.com")
        application = OrganizationMembershipRequest.objects.create(
            organization=organization, user=claimant, tier=link_tier
        )
        membership_service.approve_membership_request(
            application, decided_by=organization_owner_user, link_record=record
        )
        record.refresh_from_db()
        assert record.user_id == claimant.pk
        self._assert_moved(note, claimant, editor)

    def test_merge_closed_branch_goes_to_user(
        self,
        organization: Organization,
        make_record: RecordFactory,
        editor: RevelUser,
        linked_survivor: MembershipRecord,
    ) -> None:
        absorbed, note = self._noted_record(
            organization, make_record, editor, valid_from=_ago(30), valid_until=_ago(20)
        )
        merge_records(linked_survivor, absorbed)
        self._assert_moved(note, linked_survivor.user, editor)  # type: ignore[arg-type]

    def test_merge_open_into_linked_survivor_goes_to_user(
        self,
        organization: Organization,
        make_record: RecordFactory,
        editor: RevelUser,
        linked_survivor: MembershipRecord,
    ) -> None:
        absorbed, note = self._noted_record(organization, make_record, editor, email="dup@example.com")
        merge_records(linked_survivor, absorbed)
        self._assert_moved(note, linked_survivor.user, editor)  # type: ignore[arg-type]
        assert absorbed.organizer_notes.count() == 0

    def test_merge_into_unlinked_survivor_goes_to_its_record(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser
    ) -> None:
        survivor = make_record(email="s@example.com", valid_from=_ago(5))
        absorbed, note = self._noted_record(organization, make_record, editor, email="a@example.com")
        merge_records(survivor, absorbed)
        moved = OrganizationUserNote.objects.get(pk=note.pk)
        assert (moved.record_id, moved.user_id) == (survivor.pk, None)
        assert (moved.body, moved.updated_at) == ("Record note", STAMP)

    def test_merge_into_unlinked_survivor_with_note_merges(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser
    ) -> None:
        survivor = make_record(email="s@example.com", valid_from=_ago(5))
        _stamp(_note(organization, "Survivor text", record=survivor, updated_by=editor), LATER)
        absorbed, _n = self._noted_record(organization, make_record, editor, email="a@example.com")
        merge_records(survivor, absorbed)
        note = OrganizationUserNote.objects.get()
        assert note.record_id == survivor.pk
        assert note.body.startswith("Survivor text\n\n— From register entry (2026-03-02) —\n\nRecord note")

    def test_unlink_keeps_note_with_user(
        self, organization: Organization, make_record: RecordFactory, editor: RevelUser, claimant: RevelUser
    ) -> None:
        record, note = self._noted_record(organization, make_record, editor, email=claimant.email)
        linking.auto_link_records_for_user(claimant.pk)
        unlink_record(MembershipRecord.objects.get(pk=record.pk))
        kept = OrganizationUserNote.objects.get(pk=note.pk)
        assert (kept.user_id, kept.record_id) == (claimant.pk, None)
        assert record.organizer_notes.count() == 0

    def test_no_note_is_a_noop(self, make_record: RecordFactory, claimant: RevelUser) -> None:
        transfer_record_note(make_record(), to_user_id=claimant.pk)
        assert not OrganizationUserNote.objects.exists()

    def test_exactly_one_target_required(self, make_record: RecordFactory) -> None:
        with pytest.raises(ValueError):
            transfer_record_note(make_record())


class TestMergeIntoExistingNote:
    def _setup(
        self,
        organization: Organization,
        make_record: RecordFactory,
        user: RevelUser,
        editor: RevelUser,
        other_editor: RevelUser,
        user_stamp: datetime,
        record_stamp: datetime,
    ) -> tuple[MembershipRecord, OrganizationUserNote]:
        record = make_record()
        _stamp(_note(organization, "Record note", record=record, updated_by=other_editor), record_stamp)
        user_note = _stamp(_note(organization, "User text", user=user, updated_by=editor), user_stamp)
        return record, user_note

    def test_appends_with_dated_separator_user_text_first(
        self,
        ny_organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        editor: RevelUser,
        other_editor: RevelUser,
    ) -> None:
        record, user_note = self._setup(ny_organization, make_record, claimant, editor, other_editor, LATER, STAMP)
        transfer_record_note(record, to_user_id=claimant.pk)
        user_note.refresh_from_db()
        # 02:30 UTC on 2026-03-02 is still 2026-03-01 in New York.
        assert user_note.body == "User text\n\n— From register entry (2026-03-01) —\n\nRecord note"
        assert OrganizationUserNote.objects.count() == 1

    def test_user_note_later_keeps_its_author_and_stamp(
        self,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        editor: RevelUser,
        other_editor: RevelUser,
    ) -> None:
        record, user_note = self._setup(organization, make_record, claimant, editor, other_editor, LATER, STAMP)
        transfer_record_note(record, to_user_id=claimant.pk)
        user_note.refresh_from_db()
        assert (user_note.updated_by_id, user_note.updated_at) == (editor.pk, LATER)

    def test_record_note_later_wins_author_and_stamp(
        self,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
        editor: RevelUser,
        other_editor: RevelUser,
    ) -> None:
        record, user_note = self._setup(organization, make_record, claimant, editor, other_editor, STAMP, LATER)
        transfer_record_note(record, to_user_id=claimant.pk)
        user_note.refresh_from_db()
        assert (user_note.updated_by_id, user_note.updated_at) == (other_editor.pk, LATER)


class TestTruncation:
    def _merge(
        self, organization: Organization, make_record: RecordFactory, user: RevelUser, user_text: str, record_text: str
    ) -> tuple[OrganizationUserNote, list[t.Any]]:
        record = make_record()
        _stamp(_note(organization, record_text, record=record), STAMP)
        _note(organization, user_text, user=user)
        with capture_logs() as logs:
            transfer_record_note(record, to_user_id=user.pk)
        return OrganizationUserNote.objects.get(), logs

    def test_appended_part_is_truncated_to_the_cap(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        user_text = "u" * 4000
        note, logs = self._merge(organization, make_record, claimant, user_text, "r" * 3000)
        assert len(note.body) == USER_NOTE_MAX_LENGTH
        assert note.body.startswith(user_text + "\n\n— From register entry (2026-03-02) —")
        assert note.body.endswith("… [truncated]")
        event = next(e for e in logs if e["event"] == "organizer_note_transfer_truncated")
        assert event["dropped"] is False
        assert "r" * 10 not in str(event)

    def test_no_room_keeps_target_unchanged(
        self, organization: Organization, make_record: RecordFactory, claimant: RevelUser
    ) -> None:
        user_text = "u" * (USER_NOTE_MAX_LENGTH - 10)
        note, logs = self._merge(organization, make_record, claimant, user_text, "record text")
        assert note.body == user_text
        assert next(e for e in logs if e["event"] == "organizer_note_transfer_truncated")["dropped"] is True
        assert not OrganizationUserNote.objects.filter(record__isnull=False).exists()
