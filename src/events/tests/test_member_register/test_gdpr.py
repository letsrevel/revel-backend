"""GDPR export rules for the member register."""

import json
import typing as t
import zipfile
from io import BytesIO

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from accounts.service import gdpr
from events.models import MembershipRecord, Organization

pytestmark = pytest.mark.django_db


def _export(user: RevelUser) -> dict[str, t.Any]:
    """Generate an export and return the parsed JSON."""
    export = gdpr.generate_user_data_export(user)
    with zipfile.ZipFile(BytesIO(export.file.read())) as zip_file:
        data: dict[str, t.Any] = json.loads(zip_file.read("revel_user_data.json").decode())
    return data


def _record(organization: Organization, user: RevelUser | None, full_name: str, **extra: t.Any) -> MembershipRecord:
    return MembershipRecord.objects.create(
        organization=organization,
        user=user,
        full_name=full_name,
        valid_from=timezone.now(),
        source=MembershipRecord.Source.MANUAL,
        **extra,
    )


def test_export_includes_own_membership_records(
    organization: Organization, member_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """The member's own register rows are exported, without the staff actor."""
    _record(organization, member_user, "Own Register Name", created_by=organization_owner_user)

    data = _export(member_user)

    rows = data["membership_records"]
    assert [row["full_name"] for row in rows] == ["Own Register Name"]
    assert "created_by" not in rows[0]


def test_export_excludes_records_created_as_staff(
    organization: Organization, member_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """Rows about someone else that the user created as staff are third-party data."""
    _record(organization, member_user, "Someone Else", created_by=organization_owner_user)

    data = _export(organization_owner_user)

    assert "Someone Else" not in json.dumps(data)
    assert "created_membership_records" not in data
