"""A ban committing around a day-membership grant is enforced after both commit (two-sided post-commit recheck)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Blacklist, MembershipRecord, Organization, Ticket, TicketTier
from events.service import blacklist_service, day_membership_service
from events.utils.blacklist import get_hard_blacklisted_user_ids

from .conftest import records_for

pytestmark = pytest.mark.django_db

Reason = MembershipRecord.EndedReason


def _blind_first_ban_check(real: t.Callable[..., bool]) -> t.Callable[..., bool]:
    """Stand-in for ``_is_banned`` that reports "not banned" once (the in-transaction check that ran too early)."""
    calls: list[int] = []

    def check(organization_id: t.Any, user_id: t.Any) -> bool:
        if not calls:
            calls.append(1)
            return False
        return real(organization_id, user_id)

    return check


def test_grant_that_missed_a_committing_ban_is_revoked_after_commit(
    organization: Organization,
    organization_owner_user: RevelUser,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    holder: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    Blacklist.objects.create(organization=organization, email=holder.email, created_by=organization_owner_user)
    real = day_membership_service._is_banned
    with patch.object(day_membership_service, "_is_banned", _blind_first_ban_check(real)):
        with django_capture_on_commit_callbacks(execute=True):
            ticket = make_ticket(day_tier)
            assert [r.ended_reason for r in records_for(ticket)] == [None]

    assert [r.ended_reason for r in records_for(ticket)] == [Reason.REVOKED]


def test_bulk_grant_that_missed_a_committing_ban_is_revoked_after_commit(
    organization: Organization,
    organization_owner_user: RevelUser,
    day_tier: TicketTier,
    holder: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    Blacklist.objects.create(organization=organization, email=holder.email, created_by=organization_owner_user)
    ticket = Ticket.objects.bulk_create(
        [Ticket(event=day_tier.event, user=holder, tier=day_tier, status=Ticket.TicketStatus.ACTIVE, guest_name="D")]
    )[0]
    real = get_hard_blacklisted_user_ids
    calls: list[int] = []

    def blind_once(*args: t.Any, **kwargs: t.Any) -> t.Any:
        if not calls:
            calls.append(1)
            return RevelUser.objects.none()
        return real(*args, **kwargs)

    with patch("events.service.day_membership_service.get_hard_blacklisted_user_ids", blind_once):
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            day_membership_service.on_tickets_issued([ticket])
            assert [r.ended_reason for r in records_for(ticket)] == [None]
        assert callbacks

    assert [r.ended_reason for r in records_for(ticket)] == [Reason.REVOKED]


def test_ban_whose_revoke_ran_before_the_record_existed_revokes_it_on_commit(
    organization: Organization,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    holder: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with patch.object(day_membership_service, "_is_banned", return_value=False):
        with django_capture_on_commit_callbacks(execute=True):
            blacklist_service.apply_blacklist_consequences(holder, organization)  # revoke finds no record yet
            ticket = make_ticket(day_tier)  # a concurrent grant that the ban's in-transaction revoke never saw
            assert [r.ended_reason for r in records_for(ticket)] == [None]

    assert [r.ended_reason for r in records_for(ticket)] == [Reason.REVOKED]
