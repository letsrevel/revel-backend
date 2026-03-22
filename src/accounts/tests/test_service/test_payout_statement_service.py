"""Tests for accounts.service.payout_statement_service.

Covers:
- B2B referrer (validated VAT ID) generates self-billing invoice (Gutschrift)
- B2C referrer (no VAT ID) generates payout statement
- Correct VAT breakdown for each case
- PDF is generated and attached via WeasyPrint (mocked)
- Sequential document numbering (RVL-RP-YYYY-NNNNNN)
- Referrer and platform data snapshots
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import (
    Referral,
    ReferralCode,
    ReferralPayout,
    ReferralPayoutStatement,
    RevelUser,
    UserBillingProfile,
)
from accounts.service.payout_statement_service import (
    DOCUMENT_NUMBER_PREFIX,
    generate_payout_statement,
)
from common.models import SiteSettings

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def referrer(django_user_model: type[RevelUser]) -> RevelUser:
    """A referrer user with Stripe connected."""
    return django_user_model.objects.create_user(
        username="referrer@example.com",
        email="referrer@example.com",
        password="pass",
        first_name="Referrer",
        last_name="User",
        preferred_name="Referrer User",
        stripe_account_id="acct_referrer_001",
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )


@pytest.fixture
def referred_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who signed up via a referral code."""
    return django_user_model.objects.create_user(
        username="referred@example.com",
        email="referred@example.com",
        password="pass",
    )


@pytest.fixture
def referral_code(referrer: RevelUser) -> ReferralCode:
    """Active referral code for the referrer."""
    return ReferralCode.objects.create(user=referrer, code="REF001")


@pytest.fixture
def referral(referral_code: ReferralCode, referred_user: RevelUser) -> Referral:
    """A referral link between referrer and referred user."""
    return Referral.objects.create(
        referral_code=referral_code,
        referred_user=referred_user,
        revenue_share_percent=Decimal("15.00"),
    )


@pytest.fixture
def b2b_billing_profile(referrer: RevelUser) -> UserBillingProfile:
    """B2B billing profile with validated VAT ID (qualifies for Gutschrift)."""
    return UserBillingProfile.objects.create(
        user=referrer,
        billing_name="Referrer GmbH",
        vat_id="DE123456789",
        vat_country_code="DE",
        vat_id_validated=True,
        billing_address="Musterstr. 1, 10115 Berlin",
        billing_email="billing@referrer.de",
        self_billing_agreed=True,
    )


@pytest.fixture
def b2c_billing_profile(referrer: RevelUser) -> UserBillingProfile:
    """B2C billing profile without VAT ID (payout statement)."""
    return UserBillingProfile.objects.create(
        user=referrer,
        billing_name="Referrer Person",
        vat_id="",
        vat_country_code="AT",
        vat_id_validated=False,
        billing_address="Hauptstr. 5, 1010 Wien",
        billing_email="",
        self_billing_agreed=True,
    )


@pytest.fixture
def site_settings() -> SiteSettings:
    """Populate SiteSettings singleton with Austrian platform business details."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel GmbH"
    site.platform_business_address = "Mariahilfer Str. 10, 1060 Wien, Austria"
    site.platform_vat_id = "ATU12345678"
    site.platform_vat_country = "AT"
    site.platform_vat_rate = Decimal("20.00")
    site.save()
    return site


@pytest.fixture
def payout(referral: Referral) -> ReferralPayout:
    """A calculated referral payout for January 2026."""
    return ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("15.00"),
        currency="EUR",
        status=ReferralPayout.Status.CALCULATED,
    )


# ---------------------------------------------------------------------------
# B2B referrer: self-billing invoice (Gutschrift)
# ---------------------------------------------------------------------------


class TestB2BReferrerSelfBillingInvoice:
    """Test statement generation for B2B referrers with validated VAT ID."""

    @patch("common.service.invoice_utils.HTML")
    def test_generates_self_billing_invoice_for_b2b_referrer(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """B2B referrer with validated VAT ID gets a self-billing invoice (Gutschrift)."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.document_type == ReferralPayoutStatement.DocumentType.SELF_BILLING_INVOICE

    @patch("common.service.invoice_utils.HTML")
    def test_b2b_eu_cross_border_reverse_charge_vat_breakdown(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """German B2B referrer (EU cross-border with valid VAT): reverse charge.

        The payout_amount is EUR 15.00. With reverse charge, net = gross = 15.00,
        VAT = 0.00.
        """
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.amount_gross == Decimal("15.00")
        assert statement.amount_net == Decimal("15.00")
        assert statement.amount_vat == Decimal("0.00")
        assert statement.vat_rate == Decimal("0.00")
        assert statement.reverse_charge is True

    @patch("common.service.invoice_utils.HTML")
    def test_b2b_same_country_domestic_vat_breakdown(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Austrian B2B referrer (same country): domestic VAT 20% extracted.

        15.00 / 1.20 = 12.50, VAT = 2.50.
        """
        mock_html_cls.return_value.write_pdf.return_value = None
        # Create Austrian B2B profile
        UserBillingProfile.objects.create(
            user=referrer,
            billing_name="Austrian B2B Referrer GmbH",
            vat_id="ATU99999999",
            vat_country_code="AT",
            vat_id_validated=True,
            billing_address="Ringstr. 1, 1010 Wien",
            self_billing_agreed=True,
        )

        statement = generate_payout_statement(payout)

        assert statement.document_type == ReferralPayoutStatement.DocumentType.SELF_BILLING_INVOICE
        assert statement.amount_gross == Decimal("15.00")
        assert statement.amount_net == Decimal("12.50")
        assert statement.amount_vat == Decimal("2.50")
        assert statement.vat_rate == Decimal("20.00")
        assert statement.reverse_charge is False


# ---------------------------------------------------------------------------
# B2C referrer: payout statement
# ---------------------------------------------------------------------------


class TestB2CReferrerPayoutStatement:
    """Test statement generation for B2C/individual referrers without VAT ID."""

    @patch("common.service.invoice_utils.HTML")
    def test_generates_payout_statement_for_b2c_referrer(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2c_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """B2C referrer without VAT ID gets a payout statement (not Gutschrift)."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.document_type == ReferralPayoutStatement.DocumentType.PAYOUT_STATEMENT

    @patch("common.service.invoice_utils.HTML")
    def test_b2c_no_vat_breakdown(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2c_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """B2C referrer: no VAT applicable, gross = net = payout_amount."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.amount_gross == Decimal("15.00")
        assert statement.amount_net == Decimal("15.00")
        assert statement.amount_vat == Decimal("0.00")
        assert statement.vat_rate == Decimal("0.00")
        assert statement.reverse_charge is False


# ---------------------------------------------------------------------------
# Document numbering
# ---------------------------------------------------------------------------


class TestSequentialNumbering:
    """Test sequential document numbering."""

    @patch("common.service.invoice_utils.HTML")
    def test_first_statement_gets_number_000001(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2c_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """First statement for a year gets sequential number 000001."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.document_number == "RVL-RP-2026-000001"

    @patch("common.service.invoice_utils.HTML")
    def test_sequential_numbering_increments(
        self,
        mock_html_cls: MagicMock,
        referral: Referral,
        b2c_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Second statement for the same year increments the sequence."""
        mock_html_cls.return_value.write_pdf.return_value = None

        payout_jan = ReferralPayout.objects.create(
            referral=referral,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            net_platform_fees=Decimal("100.00"),
            payout_amount=Decimal("15.00"),
            currency="EUR",
            status=ReferralPayout.Status.CALCULATED,
        )
        stmt1 = generate_payout_statement(payout_jan)

        payout_feb = ReferralPayout.objects.create(
            referral=referral,
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            net_platform_fees=Decimal("80.00"),
            payout_amount=Decimal("12.00"),
            currency="EUR",
            status=ReferralPayout.Status.CALCULATED,
        )
        stmt2 = generate_payout_statement(payout_feb)

        assert stmt1.document_number == "RVL-RP-2026-000001"
        assert stmt2.document_number == "RVL-RP-2026-000002"

    @patch("common.service.invoice_utils.HTML")
    def test_document_number_format(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2c_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Document number follows the RVL-RP-YYYY-NNNNNN format."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        number = statement.document_number
        assert number.startswith(f"{DOCUMENT_NUMBER_PREFIX}2026-")
        seq_part = number.split("-")[-1]
        assert len(seq_part) == 6
        assert seq_part.isdigit()


# ---------------------------------------------------------------------------
# Snapshot and PDF generation
# ---------------------------------------------------------------------------


class TestSnapshotsAndPDF:
    """Test that referrer and platform data is snapshotted, and PDF is generated."""

    @patch("common.service.invoice_utils.HTML")
    def test_snapshots_referrer_data(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Statement snapshots the referrer's billing details at generation time."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.referrer_name == "Referrer GmbH"
        assert statement.referrer_address == "Musterstr. 1, 10115 Berlin"
        assert statement.referrer_vat_id == "DE123456789"
        assert statement.referrer_country == "DE"

    @patch("common.service.invoice_utils.HTML")
    def test_snapshots_referrer_display_name_when_no_billing_name(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """When billing_name is empty, falls back to referrer's display name."""
        mock_html_cls.return_value.write_pdf.return_value = None
        UserBillingProfile.objects.create(
            user=referrer,
            billing_name="",
            vat_id="",
            vat_country_code="AT",
            vat_id_validated=False,
            self_billing_agreed=True,
        )

        statement = generate_payout_statement(payout)

        # Falls back to get_display_name() which returns preferred_name
        assert statement.referrer_name == referrer.get_display_name()

    @patch("common.service.invoice_utils.HTML")
    def test_snapshots_platform_data(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Statement snapshots platform business details from SiteSettings."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.platform_business_name == "Revel GmbH"
        assert statement.platform_business_address == "Mariahilfer Str. 10, 1060 Wien, Austria"
        assert statement.platform_vat_id == "ATU12345678"

    @patch("common.service.invoice_utils.HTML")
    def test_pdf_generated_and_saved(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """A PDF is generated via WeasyPrint and saved to the statement's pdf_file field."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.pdf_file
        mock_html_cls.assert_called_once()

    @patch("common.service.invoice_utils.HTML")
    def test_pdf_filename_matches_document_number(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """PDF filename is {document_number}.pdf."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        # Django storage may add a dedup suffix (e.g. _3AJjiBH) before .pdf
        assert statement.document_number in statement.pdf_file.name
        assert statement.pdf_file.name.endswith(".pdf")

    @patch("common.service.invoice_utils.HTML")
    def test_issued_at_is_set(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """The issued_at timestamp is set to the current time at generation."""
        mock_html_cls.return_value.write_pdf.return_value = None

        before = timezone.now()
        statement = generate_payout_statement(payout)
        after = timezone.now()

        assert statement.issued_at is not None
        assert before <= statement.issued_at <= after

    @patch("common.service.invoice_utils.HTML")
    def test_currency_from_payout(
        self,
        mock_html_cls: MagicMock,
        payout: ReferralPayout,
        b2b_billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Statement currency comes from the payout."""
        mock_html_cls.return_value.write_pdf.return_value = None

        statement = generate_payout_statement(payout)

        assert statement.currency == "EUR"
