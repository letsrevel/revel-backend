"""Admin rendering for AttendeeInvoice (#897).

The VAT column must not claim a single rate for a mixed-rate cart -- the same
guard the PDF totals row carries.
"""

import typing as t
from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.admin.attendee_invoice import AttendeeInvoiceAdmin
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict
from events.tests.test_attendee_invoice._helpers import _create_issued


def _line(rate: str) -> InvoiceLineItemDict:
    """A stored line item carrying the given VAT rate."""
    return InvoiceLineItemDict(
        description="Event — Tier — Guest",
        unit_price_gross="100.00",
        discount_amount="0.00",
        net_amount="81.97",
        vat_amount="18.03",
        vat_rate=rate,
    )


def _admin() -> AttendeeInvoiceAdmin:
    """The registered admin class, instantiated off a bare site."""
    return AttendeeInvoiceAdmin(AttendeeInvoice, AdminSite())


class TestVatDisplay:
    """``AttendeeInvoiceAdmin.vat_display`` branches like the PDF totals row."""

    def test_single_rate_shows_the_rate(self) -> None:
        """The ordinary invoice still names its rate."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("18.03"),
            vat_rate=Decimal("22.00"),
            reverse_charge=False,
            line_items=t.cast(t.Any, [_line("22.00")]),
        )

        assert _admin().vat_display(invoice) == "18.03 (22.00%)"

    def test_mixed_rates_show_mixed_instead_of_the_first_payments_rate(self) -> None:
        """A 22%/10% invoice must not display '22.00%' -- that rate is one line's, not the document's."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("19.93"),
            vat_rate=Decimal("22.00"),
            reverse_charge=False,
            line_items=t.cast(t.Any, [_line("22.00"), _line("10.00")]),
        )

        rendered = _admin().vat_display(invoice)

        assert rendered == "19.93 (mixed)"
        assert "22.00%" not in rendered

    def test_reverse_charge_wins_over_the_mixed_branch(self) -> None:
        """Reverse charge is checked first: no VAT was charged at any rate."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("0.00"),
            vat_rate=Decimal("22.00"),
            reverse_charge=True,
            line_items=t.cast(t.Any, [_line("22.00"), _line("10.00")]),
        )

        assert _admin().vat_display(invoice) == "RC"


_PLAIN_STATIC = {
    "STORAGES": {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
}


@pytest.mark.django_db
class TestEn16931ColumnsInTheChangeViews:
    """PR #1251 review: ``rounding_amount`` and ``totals_method`` are shown, and never editable (#1234)."""

    @staticmethod
    def _assert_read_only(html: str) -> None:
        assert "Rounding amount" in html
        assert "Totals method" in html
        assert 'name="rounding_amount"' not in html
        assert 'name="totals_method"' not in html

    @override_settings(**_PLAIN_STATIC)
    def test_invoice_change_view(self, admin_client: Client, organization: Organization, event: Event) -> None:
        invoice = _create_issued(organization, event, organization.owner)
        response = admin_client.get(reverse("admin:events_attendeeinvoice_change", args=[invoice.pk]))
        assert response.status_code == 200
        self._assert_read_only(response.content.decode())

    @override_settings(**_PLAIN_STATIC)
    def test_credit_note_change_view(self, admin_client: Client, organization: Organization, event: Event) -> None:
        invoice = _create_issued(organization, event, organization.owner)
        credit_note = AttendeeInvoiceCreditNote.objects.create(
            invoice=invoice,
            credit_note_number="ADMIN-CN-000001",
            amount_gross=Decimal("100.00"),
            amount_net=Decimal("81.97"),
            amount_vat=Decimal("18.03"),
            line_items=invoice.line_items,
            issued_at=timezone.now(),
        )
        response = admin_client.get(reverse("admin:events_attendeeinvoicecreditnote_change", args=[credit_note.pk]))
        assert response.status_code == 200
        self._assert_read_only(response.content.decode())
