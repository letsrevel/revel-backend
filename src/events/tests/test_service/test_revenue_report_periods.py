"""Period-boundary + timezone helpers (#552)."""

import datetime as dt
import typing as t
from zoneinfo import ZoneInfo

import pytest

from accounts.models import RevelUser
from events.models import Organization
from events.service import revenue_report_service as svc
from events.utils import get_organization_timezone


def _local(y: int, m: int, d: int) -> dt.datetime:
    return dt.datetime(y, m, d, 8, 0, tzinfo=ZoneInfo("Europe/Vienna"))


def test_quarterly_in_january_returns_previous_q4() -> None:
    period = svc.closed_period_for(Organization.RevenueReportCadence.QUARTERLY, _local(2026, 1, 16))
    assert period == (dt.date(2025, 10, 1), dt.date(2025, 12, 31), "2025-Q4")


def test_quarterly_in_april_returns_q1() -> None:
    period = svc.closed_period_for(Organization.RevenueReportCadence.QUARTERLY, _local(2026, 4, 16))
    assert period == (dt.date(2026, 1, 1), dt.date(2026, 3, 31), "2026-Q1")


def test_quarterly_in_non_report_month_returns_none() -> None:
    assert svc.closed_period_for(Organization.RevenueReportCadence.QUARTERLY, _local(2026, 5, 16)) is None


def test_monthly_returns_previous_month() -> None:
    period = svc.closed_period_for(Organization.RevenueReportCadence.MONTHLY, _local(2026, 1, 16))
    assert period == (dt.date(2025, 12, 1), dt.date(2025, 12, 31), "2025-12")


@pytest.mark.django_db
def test_org_timezone_falls_back_to_platform_default(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner)  # no city
    assert get_organization_timezone(org) == ZoneInfo("Europe/Vienna")
