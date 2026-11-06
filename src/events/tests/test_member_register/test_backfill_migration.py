"""Tests for the member-register backfill data migration (#1318)."""

import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization, OrganizationMember

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
