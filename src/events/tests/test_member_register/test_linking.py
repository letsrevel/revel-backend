"""Tests for linking records to accounts: the collision matrix (#1319).

Notifications are asserted by capturing ``notification_requested`` inside
``django_capture_on_commit_callbacks(execute=True)``: pytest-django rolls the wrapping transaction back, so
``on_commit`` callbacks never fire on their own.
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Blacklist, MembershipRecord, MembershipTier, Organization, OrganizationMember
from events.service.member_register_linking import LinkOutcome, link_record
from notifications.enums import NotificationType

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus


def _open(organization: Organization, user: RevelUser) -> list[MembershipRecord]:
    return list(MembershipRecord.objects.open().filter(organization=organization, user=user))


def _granted_notifications(mock_send: t.Any) -> list[t.Any]:
    return [
        call
        for call in mock_send.call_args_list
        if call.kwargs.get("notification_type") == NotificationType.MEMBERSHIP_GRANTED
    ]


def test_no_access_row_creates_active_member_at_record_tier(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    link_tier: MembershipTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    record = make_record(invite_token="tok", duplicate_claimant=None)

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    member = OrganizationMember.objects.get(organization=organization, user=claimant)
    assert (member.status, member.tier) == (Status.ACTIVE, link_tier)
    assert result.member == member
    record.refresh_from_db()
    assert record.user == claimant and record.invite_token is None
    assert (record.linked_via, record.linked_at is not None) == (MembershipRecord.LinkMethod.INVITE, True)
    assert _open(organization, claimant) == [record]
    assert len(_granted_notifications(send)) == 1


def test_no_access_row_with_tierless_record(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    record = make_record(tier=None, tier_name="")

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    assert OrganizationMember.objects.get(organization=organization, user=claimant).tier is None
    assert _open(organization, claimant) == [record]


def test_cancelled_row_is_reactivated_at_record_tier(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    link_tier: MembershipTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    other = MembershipTier.objects.create(organization=organization, name="Other")
    OrganizationMember.objects.create(organization=organization, user=claimant, tier=other, status=Status.CANCELLED)
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    member = OrganizationMember.objects.get(organization=organization, user=claimant)
    assert (member.status, member.tier) == (Status.ACTIVE, link_tier)
    assert _open(organization, claimant) == [record]
    assert len(_granted_notifications(send)) == 1


@pytest.mark.parametrize("status", [Status.ACTIVE, Status.PAUSED])
def test_live_row_without_open_record_keeps_status_and_mirror_reconciles(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    link_tier: MembershipTier,
    status: str,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A legacy member (no open record) keeps tier and status; the mirror re-opens when tiers differ."""
    other = MembershipTier.objects.create(organization=organization, name="Other")
    OrganizationMember.objects.create(organization=organization, user=claimant, tier=other, status=status)
    MembershipRecord.objects.filter(organization=organization, user=claimant).delete()  # legacy: no register row
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    member = OrganizationMember.objects.get(organization=organization, user=claimant)
    assert (member.status, member.tier) == (status, other)
    record.refresh_from_db()
    assert record.user == claimant
    assert record.ended_reason == MembershipRecord.EndedReason.TIER_CHANGED
    (current,) = _open(organization, claimant)
    assert current.tier == other
    assert _granted_notifications(send) == []


def test_banned_row_blocks_and_changes_nothing(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=claimant, status=Status.BANNED)
    record = make_record(invite_token="tok")

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.BANNED
    record.refresh_from_db()
    assert record.user is None and record.invite_token == "tok"
    assert OrganizationMember.objects.get(organization=organization, user=claimant).status == Status.BANNED


def test_hard_blacklisted_blocks_and_changes_nothing(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    Blacklist.objects.create(organization=organization, user=claimant, created_by=organization_owner_user)
    # Blacklisting also bans the access row; drop it so only the blacklist check can block the claim.
    OrganizationMember.objects.filter(organization=organization, user=claimant).delete()
    record = make_record()

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.BANNED
    record.refresh_from_db()
    assert record.user is None
    assert not OrganizationMember.objects.filter(organization=organization, user=claimant).exists()


def test_claimant_with_open_standard_record_is_a_duplicate(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser, link_tier: MembershipTier
) -> None:
    OrganizationMember.objects.create(organization=organization, user=claimant, tier=link_tier)
    existing = _open(organization, claimant)
    record = make_record(invite_token="tok")

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.DUPLICATE
    record.refresh_from_db()
    assert record.user is None and record.duplicate_claimant == claimant and record.invite_token == "tok"
    assert _open(organization, claimant) == existing


def test_linked_clears_duplicate_claimant(
    make_record: RecordFactory, claimant: RevelUser, django_user_model: type[RevelUser]
) -> None:
    other = django_user_model.objects.create_user(username="o", email="o@example.com", password="pw")
    record = make_record(duplicate_claimant=other)

    link_record(record, claimant)

    record.refresh_from_db()
    assert record.duplicate_claimant is None


def test_closed_record_sets_user_only(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    start = timezone.now() - timedelta(days=30)
    record = make_record(
        valid_from=start,
        valid_until=start + timedelta(days=10),
        ended_reason=MembershipRecord.EndedReason.CANCELLED,
        invite_token="tok",
    )

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED and result.member is None
    record.refresh_from_db()
    assert record.user == claimant and record.invite_token is None
    assert not OrganizationMember.objects.filter(organization=organization, user=claimant).exists()


def test_closed_record_links_even_for_banned_claimant(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    """Rule 2 precedes the ban check: a closed record grants nothing, so there is nothing to protect."""
    OrganizationMember.objects.create(organization=organization, user=claimant, status=Status.BANNED)
    start = timezone.now() - timedelta(days=30)
    record = make_record(valid_from=start, valid_until=start + timedelta(days=1))

    assert link_record(record, claimant).outcome == LinkOutcome.LINKED


def test_temporary_record_sets_user_only(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    record = make_record(kind=MembershipRecord.Kind.TEMPORARY)

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    record.refresh_from_db()
    assert record.user == claimant
    assert not OrganizationMember.objects.filter(organization=organization, user=claimant).exists()


def test_already_linked_changes_nothing(
    make_record: RecordFactory, claimant: RevelUser, django_user_model: type[RevelUser]
) -> None:
    owner = django_user_model.objects.create_user(username="w", email="w@example.com", password="pw")
    record = make_record(user=owner)

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.ALREADY_LINKED
    record.refresh_from_db()
    assert record.user == owner
