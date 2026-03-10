"""Tests for invoice generation workflows.

Tests cover:
- Full invoice generation for a billing period (happy path, snapshots, aggregation)
- Idempotency: duplicate calls do not create duplicate invoices
- Multi-currency support: separate invoices per currency
- Edge cases: zero fees, failed payments, payments outside period
- Reverse charge invoices
- Pre-VAT payment fallback behavior
- PDF generation via WeasyPrint (mocked)
- Monthly invoice date range calculation (generate_monthly_invoices)
"""

import typing as t
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, EventSeries
from events.models.invoice import PlatformFeeInvoice
from events.models.organization import Organization
from events.models.ticket import Payment, Ticket, TicketTier
from events.service.invoice_service import (
    generate_invoices_for_period,
    generate_monthly_invoices,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner with a real email."""
    return django_user_model.objects.create_user(
        username="gen_owner",
        email="gen_owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """User who purchases tickets."""
    return django_user_model.objects.create_user(
        username="gen_buyer",
        email="gen_buyer@example.com",
        password="pass",
    )


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Organization with VAT / billing fields populated."""
    return Organization.objects.create(
        name="Gen Invoice Org",
        slug="gen-invoice-org",
        owner=owner,
        vat_id="DE123456789",
        vat_country_code="DE",
        billing_address="Musterstr. 1, 10115 Berlin",
        billing_email="billing@gen.com",
    )


@pytest.fixture
def event_series(org: Organization) -> EventSeries:
    """Event series for invoice generation testing."""
    return EventSeries.objects.create(
        organization=org,
        name="Gen Series",
        slug="gen-series",
    )


@pytest.fixture
def gen_event(org: Organization, event_series: EventSeries) -> Event:
    """Event belonging to the org fixture."""
    now = timezone.now()
    return Event.objects.create(
        organization=org,
        name="Gen Event",
        slug="gen-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=200,
        start=now,
        end=now + timedelta(hours=4),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def tier(gen_event: Event) -> TicketTier:
    """A paid ticket tier linked to the event."""
    return TicketTier.objects.create(
        event=gen_event,
        name="Standard",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def site_settings() -> SiteSettings:
    """Populate SiteSettings singleton with platform business details."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel S.r.l."
    site.platform_business_address = "Via Roma 1, 00100 Roma, Italy"
    site.platform_vat_id = "IT12345678901"
    site.platform_vat_rate = Decimal("22.00")
    site.save()
    return site


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_ticket(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    suffix: str = "",
) -> Ticket:
    """Helper to create a Ticket with a unique guest name."""
    return Ticket.objects.create(
        event=event,
        user=user,
        tier=tier,
        guest_name=f"Guest {user.username}{suffix}",
    )


def _create_payment(
    *,
    ticket: Ticket,
    user: RevelUser,
    amount: Decimal = Decimal("25.00"),
    platform_fee: Decimal = Decimal("2.50"),
    platform_fee_net: Decimal | None = None,
    platform_fee_vat: Decimal | None = None,
    platform_fee_vat_rate: Decimal | None = None,
    platform_fee_reverse_charge: bool = False,
    currency: str = "EUR",
    status: str = Payment.PaymentStatus.SUCCEEDED,
    created_at: t.Any = None,
) -> Payment:
    """Helper to create a Payment with sensible defaults.

    Allows overriding any field relevant to invoice testing.
    """
    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=f"cs_test_{ticket.pk}",
        status=status,
        amount=amount,
        platform_fee=platform_fee,
        platform_fee_net=platform_fee_net if platform_fee_net is not None else platform_fee,
        platform_fee_vat=platform_fee_vat if platform_fee_vat is not None else Decimal("0.00"),
        platform_fee_vat_rate=platform_fee_vat_rate,
        platform_fee_reverse_charge=platform_fee_reverse_charge,
        currency=currency,
    )
    if created_at is not None:
        # Bypass auto_now_add by using queryset update
        Payment.objects.filter(pk=payment.pk).update(created_at=created_at)
        payment.refresh_from_db()
    return payment


# ===========================================================================
# generate_invoices_for_period
# ===========================================================================


class TestGenerateInvoicesForPeriod:
    """Tests for the main invoice generation function."""

    @patch("events.service.invoice_service.HTML")
    def test_creates_invoice_for_org_with_payments(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """An invoice is created for an org that has succeeded payments in the period."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 1, 1)
        period_end = date(2026, 1, 31)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_gen1")
        _create_payment(
            ticket=ticket,
            user=buyer,
            amount=Decimal("25.00"),
            platform_fee=Decimal("2.50"),
            platform_fee_net=Decimal("2.05"),
            platform_fee_vat=Decimal("0.45"),
            platform_fee_vat_rate=Decimal("22.00"),
            created_at=timezone.make_aware(datetime(2026, 1, 15, 12, 0)),
        )

        # Act
        invoices = generate_invoices_for_period(period_start, period_end)

        # Assert
        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.organization == org
        assert inv.fee_gross == Decimal("2.50")
        assert inv.fee_net == Decimal("2.05")
        assert inv.fee_vat == Decimal("0.45")
        assert inv.fee_vat_rate == Decimal("22.00")
        assert inv.currency == "EUR"
        assert inv.reverse_charge is False
        assert inv.total_tickets == 1
        assert inv.total_ticket_revenue == Decimal("25.00")
        assert inv.status == PlatformFeeInvoice.InvoiceStatus.ISSUED
        assert inv.issued_at is not None
        assert inv.invoice_number.startswith("RVL-2026-")

    @patch("events.service.invoice_service.HTML")
    def test_snapshots_org_data(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Invoice snapshots the organization's name, VAT ID, and address."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 2, 1)
        period_end = date(2026, 2, 28)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_snap")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("3.00"),
            platform_fee_net=Decimal("3.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 2, 10, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.org_name == "Gen Invoice Org"
        assert inv.org_vat_id == "DE123456789"
        assert inv.org_vat_country == "DE"
        assert inv.org_address == "Musterstr. 1, 10115 Berlin"

    @patch("events.service.invoice_service.HTML")
    def test_snapshots_platform_data(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Invoice snapshots platform business details from SiteSettings."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 3, 1)
        period_end = date(2026, 3, 31)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_plat")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("1.50"),
            platform_fee_net=Decimal("1.50"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 3, 15, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.platform_business_name == "Revel S.r.l."
        assert inv.platform_business_address == "Via Roma 1, 00100 Roma, Italy"
        assert inv.platform_vat_id == "IT12345678901"

    @patch("events.service.invoice_service.HTML")
    def test_skips_orgs_with_zero_fees(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Organizations whose total platform fees are zero are skipped."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 4, 1)
        period_end = date(2026, 4, 30)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_zero")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("0.00"),
            platform_fee_net=Decimal("0.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 4, 15, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert invoices == []

    @patch("events.service.invoice_service.HTML")
    def test_skips_failed_payments(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Only succeeded payments are included; failed/pending/refunded are excluded."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 5, 1)
        period_end = date(2026, 5, 31)

        for i, status in enumerate(
            [Payment.PaymentStatus.FAILED, Payment.PaymentStatus.PENDING, Payment.PaymentStatus.REFUNDED]
        ):
            t = _create_ticket(gen_event, tier, buyer, suffix=f"_fail{i}")
            _create_payment(
                ticket=t,
                user=buyer,
                platform_fee=Decimal("5.00"),
                platform_fee_net=Decimal("5.00"),
                platform_fee_vat=Decimal("0.00"),
                status=status,
                created_at=timezone.make_aware(datetime(2026, 5, 15, 12, 0)),
            )

        invoices = generate_invoices_for_period(period_start, period_end)
        assert invoices == []

    @patch("events.service.invoice_service.HTML")
    def test_idempotency_second_call_no_duplicate(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Calling generate_invoices_for_period twice does not create duplicates."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 6, 1)
        period_end = date(2026, 6, 30)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_idem")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("2.00"),
            platform_fee_net=Decimal("2.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 6, 15, 12, 0)),
        )

        first_run = generate_invoices_for_period(period_start, period_end)
        second_run = generate_invoices_for_period(period_start, period_end)

        assert len(first_run) == 1
        assert len(second_run) == 0
        assert (
            PlatformFeeInvoice.objects.filter(organization=org, period_start=period_start, currency="EUR").count() == 1
        )

    @patch("events.service.invoice_service.HTML")
    def test_multiple_currencies_separate_invoices(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Payments in different currencies produce separate invoices."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 7, 1)
        period_end = date(2026, 7, 31)
        created_at = timezone.make_aware(datetime(2026, 7, 15, 12, 0))

        t_eur = _create_ticket(gen_event, tier, buyer, suffix="_eur")
        _create_payment(
            ticket=t_eur,
            user=buyer,
            platform_fee=Decimal("2.00"),
            platform_fee_net=Decimal("2.00"),
            platform_fee_vat=Decimal("0.00"),
            currency="EUR",
            created_at=created_at,
        )

        t_usd = _create_ticket(gen_event, tier, buyer, suffix="_usd")
        _create_payment(
            ticket=t_usd,
            user=buyer,
            platform_fee=Decimal("3.00"),
            platform_fee_net=Decimal("3.00"),
            platform_fee_vat=Decimal("0.00"),
            currency="USD",
            created_at=created_at,
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 2
        currencies = {inv.currency for inv in invoices}
        assert currencies == {"EUR", "USD"}

    @patch("events.service.invoice_service.HTML")
    def test_aggregates_multiple_payments(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Multiple payments for the same org/currency are aggregated into one invoice."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 8, 1)
        period_end = date(2026, 8, 31)
        created_at = timezone.make_aware(datetime(2026, 8, 10, 12, 0))

        for i in range(3):
            t = _create_ticket(gen_event, tier, buyer, suffix=f"_agg{i}")
            _create_payment(
                ticket=t,
                user=buyer,
                amount=Decimal("25.00"),
                platform_fee=Decimal("2.50"),
                platform_fee_net=Decimal("2.05"),
                platform_fee_vat=Decimal("0.45"),
                platform_fee_vat_rate=Decimal("22.00"),
                created_at=created_at,
            )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.fee_gross == Decimal("7.50")
        assert inv.fee_net == Decimal("6.15")
        assert inv.fee_vat == Decimal("1.35")
        assert inv.total_tickets == 3
        assert inv.total_ticket_revenue == Decimal("75.00")

    @patch("events.service.invoice_service.HTML")
    def test_pdf_generated_and_attached(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """A PDF is generated via WeasyPrint and saved to the invoice pdf_file field."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 9, 1)
        period_end = date(2026, 9, 30)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_pdf")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("1.00"),
            platform_fee_net=Decimal("1.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 9, 15, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.pdf_file
        mock_html_cls.assert_called_once()

    @patch("events.service.invoice_service.HTML")
    def test_payments_outside_period_excluded(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Payments created outside the billing period are excluded."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 10, 1)
        period_end = date(2026, 10, 31)

        # Payment before the period
        t_before = _create_ticket(gen_event, tier, buyer, suffix="_before")
        _create_payment(
            ticket=t_before,
            user=buyer,
            platform_fee=Decimal("5.00"),
            platform_fee_net=Decimal("5.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 9, 30, 23, 59)),
        )
        # Payment after the period
        t_after = _create_ticket(gen_event, tier, buyer, suffix="_after")
        _create_payment(
            ticket=t_after,
            user=buyer,
            platform_fee=Decimal("5.00"),
            platform_fee_net=Decimal("5.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 11, 1, 0, 1)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)
        assert invoices == []

    @patch("events.service.invoice_service.HTML")
    def test_reverse_charge_invoice(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Invoice for an org where all payments use reverse charge."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 11, 1)
        period_end = date(2026, 11, 30)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_rc_inv")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("2.00"),
            platform_fee_net=Decimal("2.00"),
            platform_fee_vat=Decimal("0.00"),
            platform_fee_vat_rate=Decimal("0.00"),
            platform_fee_reverse_charge=True,
            created_at=timezone.make_aware(datetime(2026, 11, 15, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.reverse_charge is True
        assert inv.fee_vat_rate == Decimal("0.00")

    @patch("events.service.invoice_service.HTML")
    def test_invoice_number_format(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Invoice number follows the RVL-YYYY-NNNNNN format."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2026, 12, 1)
        period_end = date(2026, 12, 31)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_fmt")
        _create_payment(
            ticket=ticket,
            user=buyer,
            platform_fee=Decimal("1.00"),
            platform_fee_net=Decimal("1.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2026, 12, 15, 12, 0)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        number = invoices[0].invoice_number
        assert number.startswith("RVL-2026-")
        seq_part = number.split("-")[-1]
        assert len(seq_part) == 6
        assert seq_part.isdigit()

    @patch("events.service.invoice_service.HTML")
    def test_fee_net_fallback_for_pre_vat_payments(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """When platform_fee_net is null, fee_net falls back to fee_gross."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2025, 6, 1)
        period_end = date(2025, 6, 30)
        ticket = _create_ticket(gen_event, tier, buyer, suffix="_prevat")
        payment = Payment.objects.create(
            ticket=ticket,
            user=buyer,
            stripe_session_id=f"cs_test_prevat_{ticket.pk}",
            status=Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("25.00"),
            platform_fee=Decimal("2.50"),
            platform_fee_net=None,
            platform_fee_vat=None,
            platform_fee_vat_rate=None,
            platform_fee_reverse_charge=False,
            currency="EUR",
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.make_aware(datetime(2025, 6, 15, 12, 0)))

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.fee_gross == Decimal("2.50")
        assert inv.fee_net == Decimal("2.50")
        assert inv.fee_vat == Decimal("0.00")

    @patch("events.service.invoice_service.HTML")
    def test_period_boundary_inclusive(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        gen_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Payments on the exact first and last day of the period are included."""
        mock_html_cls.return_value.write_pdf.return_value = None
        period_start = date(2025, 3, 1)
        period_end = date(2025, 3, 31)

        # Payment at the very start of the period
        t_start = _create_ticket(gen_event, tier, buyer, suffix="_bnd_s")
        _create_payment(
            ticket=t_start,
            user=buyer,
            platform_fee=Decimal("1.00"),
            platform_fee_net=Decimal("1.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2025, 3, 1, 0, 0, 1)),
        )

        # Payment at the very end of the period
        t_end = _create_ticket(gen_event, tier, buyer, suffix="_bnd_e")
        _create_payment(
            ticket=t_end,
            user=buyer,
            platform_fee=Decimal("1.00"),
            platform_fee_net=Decimal("1.00"),
            platform_fee_vat=Decimal("0.00"),
            created_at=timezone.make_aware(datetime(2025, 3, 31, 23, 59, 59)),
        )

        invoices = generate_invoices_for_period(period_start, period_end)

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.total_tickets == 2
        assert inv.fee_gross == Decimal("2.00")


# ===========================================================================
# generate_monthly_invoices
# ===========================================================================


class TestGenerateMonthlyInvoices:
    """Tests for the monthly invoice convenience wrapper."""

    @patch("events.service.invoice_service.generate_invoices_for_period")
    @patch("events.service.invoice_service.date")
    def test_calculates_previous_month_range_mid_year(
        self,
        mock_date: MagicMock,
        mock_generate: MagicMock,
    ) -> None:
        """When called on March 1, it generates invoices for Feb 1-28."""
        mock_date.today.return_value = date(2026, 3, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_generate.return_value = []

        generate_monthly_invoices()

        mock_generate.assert_called_once_with(date(2026, 2, 1), date(2026, 2, 28))

    @patch("events.service.invoice_service.generate_invoices_for_period")
    @patch("events.service.invoice_service.date")
    def test_calculates_previous_month_range_january(
        self,
        mock_date: MagicMock,
        mock_generate: MagicMock,
    ) -> None:
        """When called on Jan 1, it generates invoices for Dec 1-31 of the previous year."""
        mock_date.today.return_value = date(2026, 1, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_generate.return_value = []

        generate_monthly_invoices()

        mock_generate.assert_called_once_with(date(2025, 12, 1), date(2025, 12, 31))

    @patch("events.service.invoice_service.generate_invoices_for_period")
    @patch("events.service.invoice_service.date")
    def test_calculates_previous_month_range_leap_year(
        self,
        mock_date: MagicMock,
        mock_generate: MagicMock,
    ) -> None:
        """When called on March 1 of a leap year, Feb has 29 days."""
        mock_date.today.return_value = date(2028, 3, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        mock_generate.return_value = []

        generate_monthly_invoices()

        mock_generate.assert_called_once_with(date(2028, 2, 1), date(2028, 2, 29))

    @patch("events.service.invoice_service.generate_invoices_for_period")
    @patch("events.service.invoice_service.date")
    def test_returns_generated_invoices(
        self,
        mock_date: MagicMock,
        mock_generate: MagicMock,
    ) -> None:
        """The return value from generate_invoices_for_period is passed through."""
        mock_date.today.return_value = date(2026, 4, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        sentinel = [MagicMock(spec=PlatformFeeInvoice)]
        mock_generate.return_value = sentinel

        result = generate_monthly_invoices()

        assert result is sentinel
