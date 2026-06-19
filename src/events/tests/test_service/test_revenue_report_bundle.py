"""Bundle builder tests for the revenue & VAT report (#551)."""

import datetime as dt
import io
import typing as t
import zipfile
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import revenue_report_service as svc


@pytest.fixture
def report_data(db: t.Any) -> svc.RevenueReportData:
    user = RevelUser.objects.create_user(username="o", email="o@example.com", password="x")
    org = Organization.objects.create(
        name="Org",
        slug="org",
        owner=user,
        vat_rate=Decimal("20.00"),
        vat_country_code="AT",
        billing_name="Org GmbH",
        vat_id="ATU12345678",
    )
    now = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
    event = Event.objects.create(
        organization=org,
        name="E",
        slug="e",
        start=now,
        end=now + dt.timedelta(hours=2),
    )
    tier = TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("120.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Alice"
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("120.00"),
        currency="EUR",
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("20.00"),
        vat_rate=Decimal("20.00"),
        platform_fee=Decimal("0.00"),
        stripe_session_id="cs_test_bundle",
    )
    scope = svc.ReportScope(org=org, event_id=None, date_from=dt.date(2026, 1, 1), date_to=dt.date(2026, 12, 31))
    return svc.build_revenue_report_data(scope)


@pytest.mark.django_db
def test_xlsx_has_summary_and_transactions_sheets(report_data: svc.RevenueReportData) -> None:
    wb = load_workbook(io.BytesIO(svc.build_xlsx(report_data)))
    assert wb.sheetnames == ["Summary", "Transactions"]
    headers = [c.value for c in wb["Transactions"][1]]
    assert "payment_id" in headers and "vat_rate" in headers and "stripe_payout_id" in headers


@pytest.mark.django_db
def test_pdf_is_nonempty_pdf(report_data: svc.RevenueReportData) -> None:
    pdf = svc.build_pdf(report_data)
    assert pdf[:4] == b"%PDF"


@pytest.mark.django_db
def test_zip_contains_exactly_xlsx_and_pdf(report_data: svc.RevenueReportData) -> None:
    with zipfile.ZipFile(io.BytesIO(svc.build_zip(report_data))) as zf:
        names = sorted(zf.namelist())
    assert len(names) == 2
    assert any(n.endswith(".xlsx") for n in names)
    assert any(n.endswith(".pdf") for n in names)
