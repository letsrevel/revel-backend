"""Bundle builder tests for the revenue & VAT report (#551)."""

import datetime as dt
import io
import typing as t
import zipfile
from decimal import Decimal

import pytest
from django.utils import timezone
from openpyxl import load_workbook

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
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
    # Wide window so the now-stamped sale always falls in-period (year-agnostic).
    scope = svc.ReportScope(org=org, event_id=None, date_from=dt.date(2000, 1, 1), date_to=dt.date(2100, 1, 1))
    return svc.build_revenue_report_data(scope)


@pytest.mark.django_db
def test_xlsx_has_summary_and_transactions_sheets(report_data: svc.RevenueReportData) -> None:
    wb = load_workbook(io.BytesIO(svc.build_xlsx(report_data)))
    assert wb.sheetnames == ["Summary", "Transactions", "Membership payments"]
    headers = [c.value for c in wb["Transactions"][1]]
    assert "payment_id" in headers and "vat_rate" in headers and "stripe_payout_id" in headers


@pytest.mark.django_db
def test_xlsx_is_styled_and_number_formatted(report_data: svc.RevenueReportData) -> None:
    """Money cells carry a thousands-separator format and the VAT label is readable (#554)."""
    wb = load_workbook(io.BytesIO(svc.build_xlsx(report_data)))
    summary = wb["Summary"]
    # Row 2 is the single 20% bucket: B=label, C=Net, E=Gross, F=Tickets.
    assert summary["B2"].value == "20%"
    assert summary["C2"].number_format == "#,##0.00"
    assert summary["F2"].number_format == "#,##0"
    assert summary.freeze_panes == "A2"
    # Header row is styled (bold white-on-blue fill applied by style_header_row).
    assert summary["A1"].font.bold is True

    txns = wb["Transactions"]
    assert txns["G2"].number_format == "#,##0.00"  # gross
    assert txns["I2"].number_format == '0.##"%"'  # vat_rate


@pytest.fixture
def membership_report_data(db: t.Any) -> svc.RevenueReportData:
    """An org whose only money is a membership payment (one refunded, one clean)."""
    owner = RevelUser.objects.create_user(username="mo", email="mo@example.com", password="x")
    member = RevelUser.objects.create_user(
        username="mm", email="member@example.com", password="x", first_name="Mia", last_name="Member"
    )
    org = Organization.objects.create(name="MOrg", slug="morg", owner=owner, vat_country_code="AT")
    tier = MembershipTier.objects.get(organization=org, name="General membership")
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="Gold", price=Decimal("30.00"), currency="EUR", period_unit="month"
    )
    now = timezone.now()
    sub = MembershipSubscription.objects.create(
        organization=org,
        user=member,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + dt.timedelta(days=30),
    )
    MembershipPayment.objects.create(
        subscription=sub,
        amount=Decimal("30.00"),
        currency="EUR",
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_start=now,
        period_end=now + dt.timedelta(days=30),
        platform_fee=Decimal("1.00"),
        refund_amount=Decimal("5.00"),
        refunded_at=now,
        stripe_invoice_id="in_test_sheet",
        stripe_payment_intent_id="pi_test_sheet",
    )
    scope = svc.ReportScope(org=org, event_id=None, date_from=dt.date(2000, 1, 1), date_to=dt.date(2100, 1, 1))
    return svc.build_revenue_report_data(scope)


@pytest.mark.django_db
def test_membership_sheet_carries_the_reconciliation_columns(
    membership_report_data: svc.RevenueReportData,
) -> None:
    """The membership sheet lists member, plan, amount, refund and both Stripe ids."""
    wb = load_workbook(io.BytesIO(svc.build_xlsx(membership_report_data)))
    sheet = wb["Membership payments"]
    assert [c.value for c in sheet[1]] == [
        "date",
        "member_email",
        "member_name",
        "plan",
        "amount",
        "currency",
        "status",
        "refund_amount",
        "stripe_invoice_id",
        "stripe_payment_intent_id",
    ]
    row = [c.value for c in sheet[2]]
    assert row[1:] == [
        "member@example.com",
        "Mia Member",
        "Gold",
        30.0,
        "EUR",
        "succeeded",
        5.0,
        "in_test_sheet",
        "pi_test_sheet",
    ]
    assert sheet["E2"].number_format == "#,##0.00"  # amount


@pytest.mark.django_db
def test_membership_only_org_still_produces_a_report(membership_report_data: svc.RevenueReportData) -> None:
    """No tickets at all: the ticket sections are empty but the ledger is not."""
    assert membership_report_data.sections == []
    assert len(membership_report_data.membership_payments) == 1


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
