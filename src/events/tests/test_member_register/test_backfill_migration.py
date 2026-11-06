"""Tests for the member-register backfill data migration (#1318)."""

import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("events.migrations.0141_backfill_membership_records")

Status = OrganizationMember.MembershipStatus


def _member(organization: Organization, user: RevelUser, status: str) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=organization, user=user, status=status)


def test_backfill_all_four_statuses(organization: Organization, django_user_model: type[RevelUser]) -> None:
    """Each status maps to the right open/closed record."""
    now = timezone.now()
    created, updated = now - timedelta(days=30), now - timedelta(days=5)
    members = {}
    for status in (Status.ACTIVE, Status.PAUSED, Status.CANCELLED, Status.BANNED):
        user = django_user_model.objects.create_user(username=f"{status}@x.com", email=f"{status}@x.com", password="p")
        members[status] = _member(organization, user, status)
        OrganizationMember.objects.filter(pk=members[status].pk).update(created_at=created, updated_at=updated)
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    assert MembershipRecord.objects.count() == 4
    for status, member in members.items():
        rec = MembershipRecord.objects.get(organization=organization, user=member.user)
        assert rec.source == MembershipRecord.Source.BACKFILL
        assert rec.kind == MembershipRecord.Kind.STANDARD
        assert rec.valid_from == created
        assert rec.email == member.user.email
        if status in (Status.ACTIVE, Status.PAUSED):
            assert rec.valid_until is None
            assert rec.ended_reason is None
        else:
            assert rec.valid_until == updated
            expected = (
                MembershipRecord.EndedReason.CANCELLED
                if status == Status.CANCELLED
                else MembershipRecord.EndedReason.EXPELLED
            )
            assert rec.ended_reason == expected


def test_backfill_skips_pairs_with_records(organization: Organization, member_user: RevelUser) -> None:
    """A member that already has a record is not duplicated."""
    _member(organization, member_user, Status.ACTIVE)
    assert MembershipRecord.objects.count() == 1

    backfill.forward(apps, None)

    assert MembershipRecord.objects.count() == 1


def test_backfill_is_rerunnable(organization: Organization, member_user: RevelUser) -> None:
    """Running twice yields the same row count."""
    _member(organization, member_user, Status.ACTIVE)
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)
    backfill.forward(apps, None)

    assert MembershipRecord.objects.count() == 1


def _backfill_user(django_user_model: type[RevelUser], name: str, **fields: str) -> RevelUser:
    return django_user_model.objects.create_user(username=name, email=f"{name}@x.com", password="p", **fields)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"first_name": "Jane", "last_name": "Doe", "preferred_name": "JD"}, "Jane Doe"),
        ({"preferred_name": "JD"}, "JD"),
        ({}, "nameuser"),
        ({"first_name": "A" * 150, "last_name": "B" * 150}, ("A" * 150 + " " + "B" * 150)[:255]),
    ],
)
def test_backfill_name_matches_runtime_snapshot(
    organization: Organization, django_user_model: type[RevelUser], fields: dict[str, str], expected: str
) -> None:
    user = _backfill_user(django_user_model, "nameuser", **fields)
    _member(organization, user, Status.ACTIVE)
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    assert MembershipRecord.objects.get(user=user).full_name == expected


def test_backfill_closed_row_with_updated_before_created(organization: Organization, member_user: RevelUser) -> None:
    """valid_until never precedes valid_from (the CheckConstraint would abort the deploy)."""
    member = _member(organization, member_user, Status.CANCELLED)
    now = timezone.now()
    OrganizationMember.objects.filter(pk=member.pk).update(created_at=now, updated_at=now - timedelta(days=1))
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    rec = MembershipRecord.objects.get(user=member_user)
    assert rec.valid_until == now == rec.valid_from


def test_backfill_skips_stranger_ban(organization: Organization, member_user: RevelUser) -> None:
    """A BANNED row created by the ban itself was never a member."""
    owner = organization.owner
    Blacklist.objects.create(organization=organization, user=member_user, created_by=owner)  # also creates the row
    assert OrganizationMember.objects.get(user=member_user).status == Status.BANNED
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    assert not MembershipRecord.objects.exists()


def test_backfill_keeps_real_member_banned_later(organization: Organization, member_user: RevelUser) -> None:
    member = _member(organization, member_user, Status.BANNED)
    now = timezone.now()
    OrganizationMember.objects.filter(pk=member.pk).update(created_at=now - timedelta(days=10))
    entry = Blacklist.objects.create(organization=organization, user=member_user, created_by=organization.owner)
    Blacklist.objects.filter(pk=entry.pk).update(created_at=now - timedelta(days=9))
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    rec = MembershipRecord.objects.get(user=member_user)
    assert rec.ended_reason == MembershipRecord.EndedReason.EXPELLED
    assert rec.valid_until is not None


def _banned_member(
    organization: Organization,
    user: RevelUser,
    *,
    entry_offset: timedelta | None = timedelta(0),
    tier: MembershipTier | None = None,
) -> OrganizationMember:
    """A BANNED row created 10 days ago whose blacklist entry sits ``entry_offset`` after the row (None: no entry)."""
    created = timezone.now() - timedelta(days=10)
    member = _member(organization, user, Status.BANNED)
    if entry_offset is not None:
        entry = Blacklist.objects.create(organization=organization, user=user, created_by=organization.owner)
        Blacklist.objects.filter(pk=entry.pk).update(created_at=created + entry_offset)
    OrganizationMember.objects.filter(pk=member.pk).update(
        created_at=created, updated_at=created + timedelta(days=1), tier=tier
    )
    member.refresh_from_db()
    MembershipRecord.objects.all().delete()
    return member


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


def _record_for(user: RevelUser) -> MembershipRecord | None:
    return MembershipRecord.objects.filter(user=user).first()


def test_backfill_stranger_ban_skipped(organization: Organization, member_user: RevelUser) -> None:
    _banned_member(organization, member_user)

    backfill.forward(apps, None)

    assert _record_for(member_user) is None


def test_backfill_keeps_tiered_member_banned_within_window(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    member = _banned_member(organization, member_user, entry_offset=timedelta(seconds=30), tier=tier)

    backfill.forward(apps, None)

    rec = MembershipRecord.objects.get(user=member_user)
    assert rec.ended_reason == MembershipRecord.EndedReason.EXPELLED
    assert rec.valid_until == member.updated_at
    assert rec.tier_name == "Gold"


@pytest.mark.parametrize("request_status", ["approved", "completed"])
def test_backfill_keeps_tierless_member_with_application(
    organization: Organization, member_user: RevelUser, request_status: str
) -> None:
    _banned_member(organization, member_user)
    OrganizationMembershipRequest.objects.create(organization=organization, user=member_user, status=request_status)

    backfill.forward(apps, None)

    assert MembershipRecord.objects.filter(user=member_user, ended_reason="expelled").count() == 1


def test_backfill_pending_application_does_not_save_stranger(
    organization: Organization, member_user: RevelUser
) -> None:
    _banned_member(organization, member_user)
    OrganizationMembershipRequest.objects.create(organization=organization, user=member_user, status="pending")

    backfill.forward(apps, None)

    assert _record_for(member_user) is None


def test_backfill_keeps_tierless_member_with_subscription(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=10,
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )
    _banned_member(organization, member_user)
    MembershipSubscription.objects.create(
        user=member_user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.CANCELLED,
    )
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    assert MembershipRecord.objects.filter(user=member_user, ended_reason="expelled").count() == 1


def test_backfill_keeps_old_member_matched_to_older_entry(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    """Telegram-style late link: the entry predates the member row, but the member has a tier."""
    _banned_member(organization, member_user, entry_offset=timedelta(days=-100), tier=tier)

    backfill.forward(apps, None)

    assert MembershipRecord.objects.filter(user=member_user, ended_reason="expelled").count() == 1


def test_backfill_entry_for_other_organization_does_not_skip(
    organization: Organization, member_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    other = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    _banned_member(organization, member_user, entry_offset=None)
    entry = Blacklist.objects.create(organization=other, user=member_user, created_by=other.owner)
    Blacklist.objects.filter(pk=entry.pk).update(created_at=timezone.now() - timedelta(days=10))
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    assert MembershipRecord.objects.filter(organization=organization, user=member_user).count() == 1


@pytest.mark.parametrize(("offset_s", "kept"), [(60, False), (61, True)])
def test_backfill_window_boundary(
    organization: Organization, member_user: RevelUser, offset_s: int, kept: bool
) -> None:
    _banned_member(organization, member_user, entry_offset=timedelta(seconds=offset_s))

    backfill.forward(apps, None)

    assert (_record_for(member_user) is not None) is kept


def test_backfill_cancelled_member_with_matching_entry_kept(organization: Organization, member_user: RevelUser) -> None:
    member = _member(organization, member_user, Status.CANCELLED)
    entry = Blacklist.objects.create(organization=organization, user=member_user, created_by=organization.owner)
    OrganizationMember.objects.filter(pk=member.pk).update(status=Status.CANCELLED)
    Blacklist.objects.filter(pk=entry.pk).update(created_at=member.created_at)
    MembershipRecord.objects.all().delete()

    backfill.forward(apps, None)

    rec = MembershipRecord.objects.get(user=member_user)
    assert rec.ended_reason == MembershipRecord.EndedReason.CANCELLED
    assert rec.valid_until is not None


def test_backfill_runs_against_historical_models(organization: Organization, member_user: RevelUser) -> None:
    """forward() must work with the migration-state registry, not only the live models."""
    _member(organization, member_user, Status.ACTIVE)
    MembershipRecord.objects.all().delete()
    state = MigrationExecutor(connection).loader.project_state(("events", "0141_backfill_membership_records"))

    backfill.forward(state.apps, None)

    rec = MembershipRecord.objects.get(user=member_user)
    assert rec.source == MembershipRecord.Source.BACKFILL
    assert rec.valid_until is None
