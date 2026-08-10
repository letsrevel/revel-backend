"""Tests for the ``report_mischarged_attendee_vat`` management command (#868).

The command CSV-lists attendee invoices issued with the pre-#868 treatment:
explicitly reverse-charged ones, and implied non-EU zero-rated exports
(total_vat=0, total_gross>0, non-EU buyer country). Free tickets and normally
taxed invoices must not appear. Uncollected VAT is estimated at the org's
CURRENT vat_rate over the invoiced amount.
"""

import csv
import io
from decimal import Decimal

import pytest
from django.core.management import call_command

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice

pytestmark = pytest.mark.django_db

_COUNTER = 0


@pytest.fixture
def org(organization: Organization) -> Organization:
    """The organization with a current VAT rate of 22%."""
    organization.vat_rate = Decimal("22.00")
    organization.save()
    return organization


def _make_invoice(
    org: Organization,
    event: Event,
    user: RevelUser,
    *,
    buyer_country: str,
    total_gross: Decimal,
    total_vat: Decimal,
    reverse_charge: bool = False,
) -> AttendeeInvoice:
    """Create an issued attendee invoice with the given VAT shape."""
    global _COUNTER  # noqa: PLW0603
    _COUNTER += 1
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_mischarge_{_COUNTER}",
        invoice_number=f"MIS-{_COUNTER:06d}",
        status=AttendeeInvoice.InvoiceStatus.ISSUED,
        total_gross=total_gross,
        total_net=total_gross - total_vat,
        total_vat=total_vat,
        vat_rate=Decimal("0.00") if total_vat == 0 else Decimal("22.00"),
        currency="EUR",
        line_items=[],
        seller_name="ACME SRL",
        seller_email="billing@acme.it",
        buyer_name="Buyer",
        buyer_email="buyer@example.com",
        buyer_vat_country=buyer_country,
        reverse_charge=reverse_charge,
    )


def _run_command() -> list[dict[str, str]]:
    """Run the command and parse its CSV output."""
    stdout = io.StringIO()
    call_command("report_mischarged_attendee_vat", stdout=stdout, stderr=io.StringIO())
    return list(csv.DictReader(io.StringIO(stdout.getvalue())))


class TestReportMischargedAttendeeVat:
    """CSV report of reverse-charged and implied-export attendee invoices."""

    def test_reverse_charged_invoice_is_listed(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        """An invoice flagged reverse_charge=True is listed with treatment 'reverse_charge'."""
        invoice = _make_invoice(
            org,
            event,
            member_user,
            buyer_country="DE",
            total_gross=Decimal("81.97"),
            total_vat=Decimal("0.00"),
            reverse_charge=True,
        )

        rows = _run_command()

        assert len(rows) == 1
        assert rows[0]["invoice_number"] == invoice.invoice_number
        assert rows[0]["treatment"] == "reverse_charge"
        assert rows[0]["buyer_country"] == "DE"

    def test_implied_export_invoice_is_listed(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        """A zero-rated non-EU invoice (no explicit flag) is listed as implied export."""
        invoice = _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("100.00"),
            total_vat=Decimal("0.00"),
        )

        rows = _run_command()

        assert len(rows) == 1
        assert rows[0]["invoice_number"] == invoice.invoice_number
        assert rows[0]["treatment"].startswith("export")

    def test_normal_and_free_invoices_are_not_listed(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """A normally taxed invoice and a free-ticket invoice must not appear."""
        _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("100.00"),
            total_vat=Decimal("18.03"),
        )
        _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("0.00"),
            total_vat=Decimal("0.00"),
        )

        assert _run_command() == []

    def test_estimated_uncollected_vat_uses_current_org_rate(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """estimated_uncollected_vat = total_gross * org.vat_rate / 100, at the CURRENT rate."""
        _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("81.97"),
            total_vat=Decimal("0.00"),
        )

        rows = _run_command()

        assert len(rows) == 1
        assert Decimal(rows[0]["org_current_vat_rate"]) == Decimal("22.00")
        # 81.97 * 22 / 100 = 18.0334 → 18.03
        assert Decimal(rows[0]["estimated_uncollected_vat"]) == Decimal("18.03")

    def test_virtual_event_invoices_are_not_listed(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Legitimate #869 virtual-event documents are not mischarges.

        A virtual event's cross-border EU B2B invoice is genuinely
        reverse-charged and its non-EU invoice genuinely carries zero VAT —
        neither may pollute the historical report.
        """
        event.is_virtual = True
        event.save(update_fields=["is_virtual"])
        _make_invoice(
            org,
            event,
            member_user,
            buyer_country="DE",
            total_gross=Decimal("81.97"),
            total_vat=Decimal("0.00"),
            reverse_charge=True,
        )
        _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("81.97"),
            total_vat=Decimal("0.00"),
        )

        assert _run_command() == []

    def test_invoice_with_deleted_event_is_still_listed(
        self, org: Organization, member_user: RevelUser, event: Event
    ) -> None:
        """A historical invoice whose event was deleted (SET_NULL) stays in the report."""
        invoice = _make_invoice(
            org,
            event,
            member_user,
            buyer_country="US",
            total_gross=Decimal("100.00"),
            total_vat=Decimal("0.00"),
        )
        # SET_NULL writes at the SQL level, bypassing full_clean — mirror that.
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(event=None)

        rows = _run_command()

        assert [row["invoice_number"] for row in rows] == [invoice.invoice_number]

    def test_both_treatments_are_listed_together(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        """One run reports both sets, oldest first."""
        rc = _make_invoice(
            org,
            event,
            member_user,
            buyer_country="DE",
            total_gross=Decimal("50.00"),
            total_vat=Decimal("0.00"),
            reverse_charge=True,
        )
        export = _make_invoice(
            org,
            event,
            member_user,
            buyer_country="CH",
            total_gross=Decimal("70.00"),
            total_vat=Decimal("0.00"),
        )

        rows = _run_command()

        assert [(row["invoice_number"], row["treatment"].split(" ")[0]) for row in rows] == [
            (rc.invoice_number, "reverse_charge"),
            (export.invoice_number, "export"),
        ]
