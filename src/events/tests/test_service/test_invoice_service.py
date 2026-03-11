"""Tests for invoice_service unit-level functions.

Tests cover:
- Currency formatting with various symbols and edge cases
- Sequential invoice/credit note number generation
- Invoice recipient email resolution logic
- VAT rate and reverse charge determination from payments
"""

import typing as t
from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries
from events.models.invoice import PlatformFeeCreditNote, PlatformFeeInvoice
from events.models.organization import Organization
from events.models.ticket import Payment, Ticket, TicketTier
from events.service.invoice_service import (
    _determine_vat_rate_and_reverse_charge,
    _get_next_credit_note_number,
    _get_next_invoice_number,
    format_currency,
    get_invoice_recipients,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner with a real email."""
    return django_user_model.objects.create_user(
        username="inv_owner",
        email="owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def owner_no_email(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner whose email is blank."""
    return django_user_model.objects.create_user(
        username="inv_owner_noemail",
        email="",
        password="pass",
    )


@pytest.fixture
def buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """User who purchases tickets."""
    return django_user_model.objects.create_user(
        username="inv_buyer",
        email="buyer@example.com",
        password="pass",
    )


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Organization with VAT / billing fields populated."""
    return Organization.objects.create(
        name="Invoice Test Org",
        slug="invoice-test-org",
        owner=owner,
        vat_id="DE123456789",
        vat_country_code="DE",
        billing_address="Musterstr. 1, 10115 Berlin",
        billing_email="billing@example.com",
        contact_email="contact@example.com",
    )


@pytest.fixture
def org_no_billing(owner: RevelUser) -> Organization:
    """Organization without billing_email, has contact_email."""
    return Organization.objects.create(
        name="No Billing Org",
        slug="no-billing-org",
        owner=owner,
        contact_email="contact@nobilling.com",
    )


@pytest.fixture
def org_no_emails(owner_no_email: RevelUser) -> Organization:
    """Organization whose owner has no email, and no billing/contact."""
    return Organization.objects.create(
        name="No Emails Org",
        slug="no-emails-org",
        owner=owner_no_email,
    )


@pytest.fixture
def event_series(org: Organization) -> EventSeries:
    """Event series for invoice testing."""
    return EventSeries.objects.create(
        organization=org,
        name="Invoice Series",
        slug="invoice-series",
    )


@pytest.fixture
def inv_event(org: Organization, event_series: EventSeries) -> Event:
    """Event belonging to the org fixture."""
    from datetime import timedelta

    now = timezone.now()
    return Event.objects.create(
        organization=org,
        name="Invoice Event",
        slug="invoice-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=now,
        end=now + timedelta(hours=3),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def tier(inv_event: Event) -> TicketTier:
    """A paid ticket tier linked to the event."""
    return TicketTier.objects.create(
        event=inv_event,
        name="General",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


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
) -> Payment:
    """Helper to create a Payment with sensible defaults."""
    return Payment.objects.create(
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


# ===========================================================================
# format_currency
# ===========================================================================


class TestFormatCurrency:
    """Tests for the format_currency helper function."""

    def test_eur_symbol(self) -> None:
        """EUR values are prefixed with the euro sign."""
        assert format_currency(Decimal("10.00"), "EUR") == "\u20ac10.00"

    def test_usd_symbol(self) -> None:
        """USD values are prefixed with the dollar sign."""
        assert format_currency(Decimal("10.00"), "USD") == "$10.00"

    def test_gbp_symbol(self) -> None:
        """GBP values are prefixed with the pound sign."""
        assert format_currency(Decimal("10.00"), "GBP") == "\u00a310.00"

    def test_chf_space_prefix(self) -> None:
        """CHF uses a code prefix with trailing space."""
        assert format_currency(Decimal("10.00"), "CHF") == "CHF 10.00"

    def test_unknown_currency_uses_code_prefix(self) -> None:
        """An unknown currency code is used as the prefix with a trailing space."""
        assert format_currency(Decimal("10.00"), "XYZ") == "XYZ 10.00"

    def test_two_decimal_places(self) -> None:
        """Values are always formatted with exactly two decimal places."""
        assert format_currency(Decimal("7"), "EUR") == "\u20ac7.00"
        assert format_currency(Decimal("7.1"), "EUR") == "\u20ac7.10"
        assert format_currency(Decimal("7.999"), "EUR") == "\u20ac8.00"

    def test_large_number_with_comma_grouping(self) -> None:
        """Large numbers include comma thousand separators."""
        assert format_currency(Decimal("1234567.89"), "EUR") == "\u20ac1,234,567.89"

    def test_zero_value(self) -> None:
        """Zero is formatted as 0.00."""
        assert format_currency(Decimal("0"), "EUR") == "\u20ac0.00"

    def test_float_input(self) -> None:
        """Float values are also accepted (per the type signature)."""
        result = format_currency(10.5, "USD")
        assert result == "$10.50"

    @pytest.mark.parametrize(
        ("currency", "expected_prefix"),
        [
            ("DKK", "DKK "),
            ("SEK", "SEK "),
            ("NOK", "NOK "),
            ("PLN", "PLN "),
            ("CZK", "CZK "),
            ("HUF", "HUF "),
            ("RON", "RON "),
            ("BGN", "BGN "),
        ],
    )
    def test_all_supported_currency_prefixes(self, currency: str, expected_prefix: str) -> None:
        """All currencies in CURRENCY_SYMBOLS use their expected prefix."""
        result = format_currency(Decimal("1.00"), currency)
        assert result == f"{expected_prefix}1.00"


# ===========================================================================
# _get_next_invoice_number / _get_next_credit_note_number
# ===========================================================================


class TestSequentialNumbering:
    """Tests for invoice and credit note number generation."""

    def _create_invoice(self, organization: Organization, **kwargs: t.Any) -> PlatformFeeInvoice:
        """Helper to create a PlatformFeeInvoice with required fields."""
        defaults = {
            "organization": organization,
            "fee_gross": Decimal("10.00"),
            "fee_net": Decimal("8.20"),
            "fee_vat": Decimal("1.80"),
            "fee_vat_rate": Decimal("22.00"),
            "org_name": organization.name,
            "platform_business_name": "Revel",
            "platform_business_address": "Addr",
            "platform_vat_id": "IT123",
        }
        defaults.update(kwargs)
        return PlatformFeeInvoice.objects.create(**defaults)

    def test_first_invoice_of_year(self) -> None:
        """First invoice of a year gets sequence number 000001."""
        with transaction.atomic():
            number = _get_next_invoice_number(2026)
        assert number == "RVL-2026-000001"

    def test_sequential_increment(self, organization: Organization) -> None:
        """Subsequent invoices increment the sequence."""
        with transaction.atomic():
            first = _get_next_invoice_number(2026)
        self._create_invoice(
            organization,
            invoice_number=first,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        with transaction.atomic():
            second = _get_next_invoice_number(2026)
        assert second == "RVL-2026-000002"

    def test_different_years_independent(self, organization: Organization) -> None:
        """Invoice numbers for different years are independent sequences."""
        self._create_invoice(
            organization,
            invoice_number="RVL-2025-000005",
            period_start=date(2025, 12, 1),
            period_end=date(2025, 12, 31),
        )
        with transaction.atomic():
            number_2026 = _get_next_invoice_number(2026)
        assert number_2026 == "RVL-2026-000001"

    def test_first_credit_note_of_year(self) -> None:
        """First credit note of a year gets sequence number 000001."""
        with transaction.atomic():
            number = _get_next_credit_note_number(2026)
        assert number == "RVL-CN-2026-000001"

    def test_credit_note_sequential_increment(self, organization: Organization) -> None:
        """Subsequent credit notes increment the sequence."""
        invoice = self._create_invoice(
            organization,
            invoice_number="RVL-2026-000099",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        with transaction.atomic():
            first_cn = _get_next_credit_note_number(2026)
        PlatformFeeCreditNote.objects.create(
            invoice=invoice,
            credit_note_number=first_cn,
            fee_gross=Decimal("5.00"),
            fee_net=Decimal("4.10"),
            fee_vat=Decimal("0.90"),
        )
        with transaction.atomic():
            second_cn = _get_next_credit_note_number(2026)
        assert first_cn == "RVL-CN-2026-000001"
        assert second_cn == "RVL-CN-2026-000002"

    def test_invoice_and_credit_note_sequences_independent(self, organization: Organization) -> None:
        """Invoice and credit note sequences do not interfere with each other."""
        invoice = self._create_invoice(
            organization,
            invoice_number="RVL-2026-000001",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        )
        PlatformFeeCreditNote.objects.create(
            invoice=invoice,
            credit_note_number="RVL-CN-2026-000001",
            fee_gross=Decimal("5.00"),
            fee_net=Decimal("4.10"),
            fee_vat=Decimal("0.90"),
        )
        with transaction.atomic():
            next_inv = _get_next_invoice_number(2026)
        with transaction.atomic():
            next_cn = _get_next_credit_note_number(2026)
        assert next_inv == "RVL-2026-000002"
        assert next_cn == "RVL-CN-2026-000002"


# ===========================================================================
# get_invoice_recipients
# ===========================================================================


class TestGetInvoiceRecipients:
    """Tests for resolving invoice email recipients."""

    def test_owner_and_billing_email(self, org: Organization) -> None:
        """Both the owner email and billing email are returned."""
        recipients = get_invoice_recipients(org)
        assert "owner@example.com" in recipients
        assert "billing@example.com" in recipients
        assert len(recipients) == 2

    def test_owner_and_contact_email_fallback(self, org_no_billing: Organization) -> None:
        """When billing_email is empty, contact_email is used as fallback."""
        recipients = get_invoice_recipients(org_no_billing)
        assert "owner@example.com" in recipients
        assert "contact@nobilling.com" in recipients
        assert len(recipients) == 2

    def test_deduplication_when_owner_matches_billing(self, org: Organization) -> None:
        """Duplicate emails are deduplicated."""
        org.billing_email = "owner@example.com"
        org.save()
        recipients = get_invoice_recipients(org)
        assert recipients == ["owner@example.com"]

    def test_no_billing_or_contact_returns_owner_only(self, owner: RevelUser) -> None:
        """When org has no billing_email or contact_email, only owner email is returned."""
        org_minimal = Organization.objects.create(
            name="Minimal Org",
            slug="minimal-org",
            owner=owner,
        )
        recipients = get_invoice_recipients(org_minimal)
        assert recipients == ["owner@example.com"]

    def test_no_owner_email_returns_billing_only(self, org_no_emails: Organization) -> None:
        """When owner has no email, only billing/contact email is returned."""
        org_no_emails.billing_email = "billing@no-owner.com"
        org_no_emails.save()
        recipients = get_invoice_recipients(org_no_emails)
        assert recipients == ["billing@no-owner.com"]

    def test_no_emails_at_all(self, org_no_emails: Organization) -> None:
        """When no emails are available at all, an empty list is returned."""
        recipients = get_invoice_recipients(org_no_emails)
        assert recipients == []


# ===========================================================================
# _determine_vat_rate_and_reverse_charge
# ===========================================================================


class TestDetermineVatRateAndReverseCharge:
    """Tests for VAT rate and reverse charge determination from payment records."""

    def test_all_payments_reverse_charge(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """When all payments have reverse_charge=True, result is (0.00, True)."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_rc1")
        t2 = _create_ticket(inv_event, tier, buyer, suffix="_rc2")
        _create_payment(ticket=t1, user=buyer, platform_fee_reverse_charge=True, platform_fee_vat_rate=Decimal("0.00"))
        _create_payment(ticket=t2, user=buyer, platform_fee_reverse_charge=True, platform_fee_vat_rate=Decimal("0.00"))

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("0.00")
        assert rc is True

    def test_all_payments_normal_vat(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """When no payments are reverse charge, the dominant VAT rate is returned."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_nv1")
        t2 = _create_ticket(inv_event, tier, buyer, suffix="_nv2")
        _create_payment(
            ticket=t1, user=buyer, platform_fee_vat_rate=Decimal("22.00"), platform_fee_reverse_charge=False
        )
        _create_payment(
            ticket=t2, user=buyer, platform_fee_vat_rate=Decimal("22.00"), platform_fee_reverse_charge=False
        )

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("22.00")
        assert rc is False

    def test_mixed_reverse_charge_uses_normal_vat(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """Mixed reverse charge payments: not marked as RC, dominant rate from non-RC."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_mx1")
        t2 = _create_ticket(inv_event, tier, buyer, suffix="_mx2")
        _create_payment(
            ticket=t1, user=buyer, platform_fee_vat_rate=Decimal("22.00"), platform_fee_reverse_charge=False
        )
        _create_payment(ticket=t2, user=buyer, platform_fee_vat_rate=Decimal("0.00"), platform_fee_reverse_charge=True)

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("22.00")
        assert rc is False

    def test_dominant_rate_wins(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """When multiple VAT rates are present, the most common one wins."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_d1")
        t2 = _create_ticket(inv_event, tier, buyer, suffix="_d2")
        t3 = _create_ticket(inv_event, tier, buyer, suffix="_d3")
        _create_payment(ticket=t1, user=buyer, platform_fee_vat_rate=Decimal("22.00"))
        _create_payment(ticket=t2, user=buyer, platform_fee_vat_rate=Decimal("22.00"))
        _create_payment(ticket=t3, user=buyer, platform_fee_vat_rate=Decimal("19.00"))

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("22.00")
        assert rc is False

    def test_single_payment(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """A single payment uses its own rate."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_sp")
        _create_payment(ticket=t1, user=buyer, platform_fee_vat_rate=Decimal("19.00"))

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("19.00")
        assert rc is False

    def test_no_payments_returns_zero(self) -> None:
        """An empty queryset returns (0.00, False)."""
        payments = Payment.objects.none()
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("0.00")
        assert rc is False

    def test_payments_without_vat_rate_fallback(
        self,
        inv_event: Event,
        tier: TicketTier,
        buyer: RevelUser,
    ) -> None:
        """Pre-VAT payments (null vat_rate) fall back to 0.00."""
        t1 = _create_ticket(inv_event, tier, buyer, suffix="_nv")
        _create_payment(ticket=t1, user=buyer, platform_fee_vat_rate=None)

        payments = Payment.objects.filter(ticket__event=inv_event)
        rate, rc = _determine_vat_rate_and_reverse_charge(payments)
        assert rate == Decimal("0.00")
        assert rc is False
