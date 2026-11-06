"""Tests for the OrganizationMember -> MembershipRecord mirror (#1318)."""

import pytest

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipTier, Organization, OrganizationMember
from events.service import member_register_service

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus
Reason = MembershipRecord.EndedReason


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """A tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def tier2(organization: Organization) -> MembershipTier:
    """A second tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Silver")


def _records(organization: Organization, user: RevelUser) -> list[MembershipRecord]:
    return list(
        MembershipRecord.objects.filter(organization=organization, user=user).order_by("valid_from", "created_at")
    )


def _join(
    organization: Organization, user: RevelUser, tier: MembershipTier | None, status: str = Status.ACTIVE
) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=organization, user=user, tier=tier, status=status)


def test_create_active_opens_record(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    member_user.first_name, member_user.last_name = "Jane", "Doe"
    member_user.save()
    _join(organization, member_user, tier)
    (rec,) = _records(organization, member_user)
    assert rec.valid_until is None
    assert rec.kind == MembershipRecord.Kind.STANDARD
    assert rec.source == MembershipRecord.Source.STAFF
    assert rec.full_name == "Jane Doe"
    assert rec.email == member_user.email
    assert rec.tier_name == "Gold"
    assert rec.tier_id == tier.pk


def test_create_paused_opens_record(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    _join(organization, member_user, tier, Status.PAUSED)
    assert len(_records(organization, member_user)) == 1


def test_create_banned_opens_nothing(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    _join(organization, member_user, tier, Status.BANNED)
    assert _records(organization, member_user) == []


def test_active_to_paused_and_back_no_change(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    pk = _records(organization, member_user)[0].pk
    for status in (Status.PAUSED, Status.ACTIVE):
        m.status = status
        m.save()
    (rec,) = _records(organization, member_user)
    assert rec.pk == pk
    assert rec.valid_until is None


def test_cancel_closes_with_cancelled(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    m = _join(organization, member_user, tier)
    m.status = Status.CANCELLED
    m.save()
    (rec,) = _records(organization, member_user)
    assert rec.valid_until is not None
    assert rec.ended_reason == Reason.CANCELLED


def test_ban_closes_with_expelled(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    m = _join(organization, member_user, tier)
    m.status = Status.BANNED
    m.save()
    (rec,) = _records(organization, member_user)
    assert rec.ended_reason == Reason.EXPELLED


def test_rejoin_after_cancel_opens_second_record(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    m.status = Status.CANCELLED
    m.save()
    m.status = Status.ACTIVE
    m.save()
    old, new = _records(organization, member_user)
    assert old.ended_reason == Reason.CANCELLED
    assert old.valid_until is not None
    assert new.valid_until is None


def test_reactivate_from_banned_opens_record(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    m = _join(organization, member_user, tier, Status.BANNED)
    m.status = Status.ACTIVE
    m.save()
    (rec,) = _records(organization, member_user)
    assert rec.valid_until is None


def test_tier_change_closes_and_reopens(
    organization: Organization, member_user: RevelUser, tier: MembershipTier, tier2: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    m.tier = tier2
    m.save()
    old, new = _records(organization, member_user)
    assert old.ended_reason == Reason.TIER_CHANGED
    assert old.valid_until is not None
    assert new.valid_until is None
    assert new.tier_id == tier2.pk
    assert new.tier_name == "Silver"


def test_tier_change_while_cancelled_opens_nothing(
    organization: Organization, member_user: RevelUser, tier: MembershipTier, tier2: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    m.status = Status.CANCELLED
    m.save()
    m.tier = tier2
    m.save()
    (rec,) = _records(organization, member_user)
    assert rec.ended_reason == Reason.CANCELLED


def test_resave_unchanged_is_idempotent(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    for _ in range(3):
        m.save()
    assert len(_records(organization, member_user)) == 1


def test_member_without_record_heals_on_save(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    m = _join(organization, member_user, tier)
    MembershipRecord.objects.all().delete()
    m.save()
    (rec,) = _records(organization, member_user)
    assert rec.valid_until is None


def test_suppress_mirror_skips(organization: Organization, member_user: RevelUser, tier: MembershipTier) -> None:
    m = _join(organization, member_user, tier)
    with member_register_service.suppress_mirror():
        m.status = Status.CANCELLED
        m.save()
    (rec,) = _records(organization, member_user)
    assert rec.valid_until is None


def test_record_source_context_sets_source(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    with member_register_service.record_source(MembershipRecord.Source.TOKEN):
        _join(organization, member_user, tier)
    (rec,) = _records(organization, member_user)
    assert rec.source == MembershipRecord.Source.TOKEN


def test_full_name_falls_back_to_display_name(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    member_user.first_name = member_user.last_name = ""
    member_user.save()
    _join(organization, member_user, tier)
    (rec,) = _records(organization, member_user)
    assert rec.full_name == member_user.get_display_name()
