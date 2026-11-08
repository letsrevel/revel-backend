"""Tests for the member register xlsx export (#1319)."""

import typing as t
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken
from openpyxl import load_workbook

from accounts.models import RevelUser
from common.models import FileExport
from events.models import MembershipRecord, Organization, OrganizationStaff
from events.service.export.member_register_export import (
    generate_member_register_export,
    start_member_register_export,
)

pytestmark = pytest.mark.django_db

Reason = MembershipRecord.EndedReason


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _rec(organization: Organization, name: str, **kw: t.Any) -> MembershipRecord:
    kw.setdefault("source", MembershipRecord.Source.MANUAL)
    kw.setdefault("valid_from", timezone.now() - timedelta(days=10))
    return MembershipRecord.objects.create(organization=organization, full_name=name, tier_name="Gold", **kw)


def _export(organization: Organization, user: RevelUser, active_on: t.Any = None) -> FileExport:
    """Create a pending member-register export row."""
    return FileExport.objects.create(
        requested_by=user,
        export_type=FileExport.ExportType.MEMBER_REGISTER,
        parameters={"org_id": str(organization.id), "active_on": active_on.isoformat() if active_on else None},
    )


def _rows(export: FileExport, sheet: str) -> list[tuple[t.Any, ...]]:
    export.refresh_from_db()
    assert export.status == FileExport.ExportStatus.READY
    with export.file.open("rb") as f:
        wb = load_workbook(BytesIO(f.read()))
    return list(wb[sheet].iter_rows(values_only=True))


def test_endpoint_dispatches_task(
    organization: Organization, organization_owner_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """``on_commit`` never fires inside the test transaction, so capture and execute the callbacks."""
    url = reverse("api:export_member_register", kwargs={"slug": organization.slug})
    with patch("events.tasks.generate_member_register_export_task") as task:
        with django_capture_on_commit_callbacks(execute=True):
            r = _client(organization_owner_user).post(url, data={}, content_type="application/json")
    assert r.status_code == 202
    export = FileExport.objects.get(pk=r.json()["id"])
    assert export.export_type == FileExport.ExportType.MEMBER_REGISTER
    assert export.requested_by == organization_owner_user
    assert export.parameters == {"org_id": str(organization.id), "active_on": None}
    task.delay.assert_called_once_with(str(export.id))


def test_start_dispatches_on_commit(
    organization: Organization, organization_owner_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """``on_commit`` never fires inside the test transaction, so capture and execute the callbacks."""
    day = date(2026, 6, 15)
    with patch("events.tasks.generate_member_register_export_task") as task:
        with django_capture_on_commit_callbacks(execute=True):
            export = start_member_register_export(organization, organization_owner_user, day)
    task.delay.assert_called_once_with(str(export.id))
    assert export.parameters["active_on"] == "2026-06-15"


def test_endpoint_requires_manage_members(organization: Organization, organization_staff_user: RevelUser) -> None:
    OrganizationStaff.objects.create(
        organization=organization,
        user=organization_staff_user,
        permissions={"default": {"manage_members": False}},
    )
    url = reverse("api:export_member_register", kwargs={"slug": organization.slug})
    with patch("events.tasks.generate_member_register_export_task") as task:
        r = _client(organization_staff_user).post(url, data={}, content_type="application/json")
    assert r.status_code == 403
    task.delay.assert_not_called()


def test_endpoint_rejects_last_representable_date(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """9999-12-31 can't bound a day window: 4xx at the schema, no export row, no task."""
    url = reverse("api:export_member_register", kwargs={"slug": organization.slug})
    with patch("events.tasks.generate_member_register_export_task") as task:
        r = _client(organization_owner_user).post(
            url, data={"active_on": "9999-12-31"}, content_type="application/json"
        )
    assert 400 <= r.status_code < 500
    task.delay.assert_not_called()


def test_workbook_all_records(organization: Organization, organization_owner_user: RevelUser) -> None:
    now = timezone.now()
    _rec(organization, "Ann", email="ann@example.com")
    _rec(organization, "Old", source=MembershipRecord.Source.BACKFILL, valid_until=now - timedelta(days=1))
    _rec(organization, "Tmp", kind=MembershipRecord.Kind.TEMPORARY)
    other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    _rec(other, "Elsewhere")
    export = _export(organization, organization_owner_user)

    generate_member_register_export(export.id)

    summary = dict(_rows(export, "Summary"))
    assert summary["Organization"] == organization.name
    assert summary["Members on"] == "All records"
    assert summary["Total records"] == 3
    assert summary["Open records"] == 2
    assert summary["Standard"] == 2
    assert summary["Temporary"] == 1
    rows = _rows(export, "Register")
    assert rows[0][:3] == ("Name", "Email", "Kind")
    assert len(rows) == 4
    by_name = {r[0]: r for r in rows[1:]}
    assert set(by_name) == {"Ann", "Old", "Tmp"}
    assert by_name["Old"][8] == "Yes"  # approximate dates
    assert by_name["Ann"][8] == "No"
    assert by_name["Ann"][2] == "Standard"
    assert by_name["Ann"][7] == "Manual"
    assert by_name["Ann"][9] == "No"  # no account
    assert by_name["Tmp"][2] == "Temporary"


def test_workbook_active_on_is_a_calendar_day_in_org_timezone(
    ny_organization: Organization, organization_owner_user: RevelUser
) -> None:
    """The export uses the same local-day semantics as the list filter."""
    ny = ZoneInfo("America/New_York")
    day = date(2026, 6, 15)
    midnight = datetime(2026, 6, 15, tzinfo=ny)
    next_midnight = midnight + timedelta(days=1)
    long_ago = midnight - timedelta(days=30)
    _rec(ny_organization, "Active", valid_from=long_ago)
    _rec(ny_organization, "Revoked", valid_from=long_ago, valid_until=next_midnight, ended_reason=Reason.REVOKED)
    _rec(ny_organization, "EndedAtMidnight", valid_from=long_ago, valid_until=midnight, ended_reason=Reason.REMOVED)
    _rec(ny_organization, "StartsNextDay", valid_from=next_midnight)
    _rec(ny_organization, "StartsLate", valid_from=next_midnight - timedelta(minutes=1))
    export = _export(ny_organization, organization_owner_user, active_on=day)

    generate_member_register_export(export.id)

    names = {r[0] for r in _rows(export, "Register")[1:]}
    assert names == {"Active", "StartsLate"}
    assert dict(_rows(export, "Summary"))["Members on"] == f"{day.isoformat()} (America/New_York)"


def test_formula_injection_sanitized(organization: Organization, organization_owner_user: RevelUser) -> None:
    _rec(organization, "=cmd|' /C calc'!A0", email="+x@example.com")
    export = _export(organization, organization_owner_user)

    generate_member_register_export(export.id)

    row = _rows(export, "Register")[1]
    assert not str(row[0]).startswith("=")
    assert not str(row[1]).startswith("+")


def test_failure_marks_failed(organization: Organization, organization_owner_user: RevelUser) -> None:
    export = _export(organization, organization_owner_user)
    export.parameters = {"org_id": "00000000-0000-0000-0000-000000000000", "active_on": None}
    export.save()

    with pytest.raises(Organization.DoesNotExist):
        generate_member_register_export(export.id)

    export.refresh_from_db()
    assert export.status == FileExport.ExportStatus.FAILED
    assert export.error_message is not None and export.error_message.startswith("Export failed:")


def test_merged_record_is_marked_and_not_counted_open(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    survivor = _rec(organization, "Survivor")
    absorbed = _rec(organization, "Absorbed", merged_into=survivor)
    export = _export(organization, organization_owner_user)

    generate_member_register_export(export.id)

    summary = dict(_rows(export, "Summary"))
    assert summary["Total records"] == 2
    assert summary["Open records"] == 1
    rows = _rows(export, "Register")
    assert rows[0][-1] == "Merged Into"
    by_name = {r[0]: r for r in rows[1:]}
    assert by_name["Absorbed"][-1] == str(survivor.pk)
    assert by_name["Survivor"][-1] in ("", None)
    assert absorbed.merged_into_id == survivor.pk


def test_day_filtered_export_excludes_absorbed_record(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """A fixed day well inside both records' periods keeps the test independent of the clock and timezone."""
    start = datetime(2026, 6, 1, tzinfo=UTC)
    survivor = _rec(organization, "Survivor", valid_from=start)
    _rec(organization, "Absorbed", merged_into=survivor, valid_from=start)
    export = _export(organization, organization_owner_user, active_on=date(2026, 6, 15))

    generate_member_register_export(export.id)

    assert {r[0] for r in _rows(export, "Register")[1:]} == {"Survivor"}
