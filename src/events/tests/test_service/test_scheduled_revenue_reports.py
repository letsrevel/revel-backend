"""Scheduled revenue-report delivery (#552)."""

import datetime as dt
import typing as t
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import revenue_report_service as svc


def _now_utc(y: int, m: int, d: int) -> dt.datetime:
    return dt.datetime(y, m, d, 6, 0, tzinfo=ZoneInfo("UTC"))


@pytest.fixture
def billable_org(db: t.Any) -> Organization:
    owner = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(
        name="Org",
        slug="org",
        owner=owner,
        billing_email="b@example.com",
        vat_rate=Decimal("20.00"),
        vat_country_code="AT",
        revenue_report_cadence=Organization.RevenueReportCadence.MONTHLY,
    )
    _start = dt.datetime(2025, 12, 10, 18, 0, tzinfo=ZoneInfo("UTC"))
    event = Event.objects.create(
        organization=org,
        name="E",
        slug="e",
        start=_start,
        end=_start + dt.timedelta(hours=2),
    )
    tier = TicketTier.objects.create(
        event=event, name="GA", price=Decimal("120.00"), currency="EUR", payment_method=TicketTier.PaymentMethod.ONLINE
    )
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=owner, status=Ticket.TicketStatus.ACTIVE, guest_name="Owner"
    )
    # Sale dated in the previous month (December 2025) relative to the Jan-16 run.
    payment = Payment.objects.create(
        ticket=ticket,
        user=owner,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_sched_1",
    )
    Payment.objects.filter(pk=payment.pk).update(created_at=dt.datetime(2025, 12, 10, 12, 0, tzinfo=ZoneInfo("UTC")))
    return org


@pytest.mark.django_db
def test_delivers_and_sets_idempotency_marker(billable_org: Organization) -> None:
    with patch("events.service.revenue_report_service.send_email") as mock_send:
        count = svc.deliver_scheduled_revenue_reports(_now_utc(2026, 1, 16))
    assert count == 1
    mock_send.delay.assert_called_once()
    billable_org.refresh_from_db()
    assert billable_org.last_revenue_report_sent_period == "2025-12"


@pytest.mark.django_db
def test_second_run_same_period_is_idempotent(billable_org: Organization) -> None:
    with patch("events.service.revenue_report_service.send_email"):
        svc.deliver_scheduled_revenue_reports(_now_utc(2026, 1, 16))
    with patch("events.service.revenue_report_service.send_email") as mock_send:
        count = svc.deliver_scheduled_revenue_reports(_now_utc(2026, 1, 16))
    assert count == 0
    mock_send.delay.assert_not_called()


@pytest.mark.django_db
def test_empty_period_is_skipped(db: t.Any) -> None:
    owner = RevelUser.objects.create_user(username="o2", email="o2@example.com", password="x")
    Organization.objects.create(
        name="Empty",
        slug="empty",
        owner=owner,
        billing_email="e@example.com",
        revenue_report_cadence=Organization.RevenueReportCadence.MONTHLY,
    )
    with patch("events.service.revenue_report_service.send_email") as mock_send:
        count = svc.deliver_scheduled_revenue_reports(_now_utc(2026, 1, 16))
    assert count == 0
    mock_send.delay.assert_not_called()
