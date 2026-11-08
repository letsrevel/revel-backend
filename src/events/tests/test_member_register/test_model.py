"""Tests for the MembershipRecord register model."""

import typing as t
from datetime import UTC, date, datetime, timedelta

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipRecordEndedReason, MembershipRecordKind, Organization
from events.utils import get_organization_day_bounds

pytestmark = pytest.mark.django_db

Reason = MembershipRecord.EndedReason


def _record(
    organization: Organization,
    user: RevelUser | None,
    *,
    kind: MembershipRecordKind = MembershipRecord.Kind.STANDARD,
    valid_from: t.Any = None,
    valid_until: t.Any = None,
    ended_reason: MembershipRecordEndedReason | None = None,
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


def test_active_during_boundaries(organization: Organization) -> None:
    """active_during overlaps the half-open window [start, end), skips REVOKED, keeps open-ended."""
    start = timezone.now()
    end = start + timedelta(days=1)
    minute = timedelta(minutes=1)
    starts_at_end = _record(organization, None, valid_from=end)
    ends_at_start = _record(
        organization, None, valid_from=start - timedelta(days=5), valid_until=start, ended_reason=Reason.REMOVED
    )
    starts_before_end = _record(organization, None, valid_from=end - minute)
    ends_after_start = _record(
        organization,
        None,
        valid_from=start - timedelta(days=5),
        valid_until=start + minute,
        ended_reason=Reason.REMOVED,
    )
    open_ = _record(organization, None, valid_from=start - timedelta(days=5))
    revoked = _record(
        organization,
        None,
        valid_from=start - timedelta(days=5),
        valid_until=end + timedelta(days=5),
        ended_reason=Reason.REVOKED,
    )

    got = set(MembershipRecord.objects.filter(organization=organization).active_during(start, end))

    assert got == {starts_before_end, ends_after_start, open_}
    assert not {starts_at_end, ends_at_start, revoked} & got


def test_day_bounds_default_timezone(organization: Organization) -> None:
    """An org without a city uses settings.TIME_ZONE."""
    start, end = get_organization_day_bounds(organization, date(2026, 6, 15))
    assert str(start.tzinfo) == settings.TIME_ZONE
    assert (start.hour, start.minute, start.day) == (0, 0, 15)
    assert (end.hour, end.minute, end.day) == (0, 0, 16)


def test_day_bounds_org_timezone(ny_organization: Organization) -> None:
    """Bounds are local midnights in the org city's timezone."""
    start, end = get_organization_day_bounds(ny_organization, date(2026, 6, 15))
    assert start.utcoffset() == timedelta(hours=-4)
    assert end.utcoffset() == timedelta(hours=-4)
    assert (start.hour, end.hour, end.day) == (0, 0, 16)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=24)


@pytest.mark.parametrize(
    ("day", "start_utc", "hours"),
    [
        # Havana springs forward at local midnight (00:00 -> 01:00): the day starts at the jump.
        (date(2025, 3, 9), datetime(2025, 3, 9, 5, tzinfo=UTC), 23),
        # Havana falls back at 01:00 -> 00:00: midnight happens twice; the day starts at the first one.
        (date(2025, 11, 2), datetime(2025, 11, 2, 4, tzinfo=UTC), 25),
    ],
)
def test_day_bounds_when_midnight_is_skipped_or_repeated(
    ny_organization: Organization, day: date, start_utc: datetime, hours: int
) -> None:
    """fold=0 on a nonexistent/ambiguous local midnight maps to the first instant of the local day (PEP 495).

    Historical transitions, so a future tzdata change to Cuba's rules can't break the test.
    """
    assert ny_organization.city is not None
    ny_organization.city.timezone = "America/Havana"
    ny_organization.city.save(update_fields=["timezone"])
    start, end = get_organization_day_bounds(ny_organization, day)
    assert start.astimezone(UTC) == start_utc
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=hours)


def test_day_bounds_dst_spring_forward(ny_organization: Organization) -> None:
    """The DST changeover day (2026-03-08 in New York) is 23 hours long."""
    start, end = get_organization_day_bounds(ny_organization, date(2026, 3, 8))
    assert start.utcoffset() == timedelta(hours=-5)
    assert end.utcoffset() == timedelta(hours=-4)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)
