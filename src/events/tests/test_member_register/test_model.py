"""Tests for the MembershipRecord register model."""

import typing as t
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization

pytestmark = pytest.mark.django_db


def _record(
    organization: Organization,
    user: RevelUser | None,
    *,
    kind: MembershipRecord.Kind = MembershipRecord.Kind.STANDARD,
    valid_from: t.Any = None,
    valid_until: t.Any = None,
    ended_reason: MembershipRecord.EndedReason | None = None,
) -> MembershipRecord:
    return MembershipRecord.objects.create(
        organization=organization,
        user=user,
        full_name="Jane Doe",
        kind=kind,
        valid_from=valid_from or timezone.now(),
        valid_until=valid_until,
        ended_reason=ended_reason,
        source=MembershipRecord.Source.MANUAL,
    )


def test_second_open_standard_record_rejected(organization: Organization, organization_owner_user: RevelUser) -> None:
    """Only one open STANDARD record per (org, user)."""
    _record(organization, organization_owner_user)
    with pytest.raises(ValidationError):
        _record(organization, organization_owner_user)


def test_open_standard_unique_ignores_null_user_and_temporary(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Null users and TEMPORARY records are exempt from the open-standard uniqueness."""
    _record(organization, None)
    _record(organization, None)
    _record(organization, organization_owner_user)
    _record(organization, organization_owner_user, kind=MembershipRecord.Kind.TEMPORARY)
    assert MembershipRecord.objects.filter(organization=organization).count() == 4


def test_valid_until_before_valid_from_rejected(organization: Organization) -> None:
    """valid_until must not precede valid_from."""
    now = timezone.now()
    with pytest.raises(ValidationError):
        _record(organization, None, valid_from=now, valid_until=now - timedelta(days=1))


def test_active_on_boundaries(organization: Organization) -> None:
    """active_on is half-open [valid_from, valid_until), open-ended aware, and skips REVOKED."""
    t0 = timezone.now()
    t1 = t0 + timedelta(days=30)
    closed = _record(
        organization, None, valid_from=t0, valid_until=t1, ended_reason=MembershipRecord.EndedReason.REMOVED
    )
    open_ = _record(organization, None, valid_from=t0)
    revoked = _record(
        organization,
        None,
        kind=MembershipRecord.Kind.TEMPORARY,
        valid_from=t0,
        valid_until=t1,
        ended_reason=MembershipRecord.EndedReason.REVOKED,
    )
    qs = MembershipRecord.objects.filter(organization=organization)

    at_t0 = set(qs.active_on(t0))
    assert at_t0 == {closed, open_}
    assert revoked not in qs.active_on(t0 + timedelta(days=1))
    at_t1 = set(qs.active_on(t1))
    assert closed not in at_t1 and open_ in at_t1
    assert open_ in qs.active_on(t0 + timedelta(days=3650))
    assert set(qs.open()) == {open_}
