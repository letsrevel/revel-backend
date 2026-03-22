"""Tests for the process_referral_payouts Celery task.

Covers:
- Pre-flight checks: skips payouts when referrer lacks Stripe charges,
  billing profile, or self-billing agreement.
- Happy path: creates Stripe transfer, updates status to PAID, stores transfer ID.
- Stripe error: updates status to FAILED, propagates exception for Celery retry.
- Email dispatch with correct from_email, reply_to, and attachment.
- Multiple payouts processed in order.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from accounts.models import (
    Referral,
    ReferralCode,
    ReferralPayout,
    ReferralPayoutStatement,
    RevelUser,
    UserBillingProfile,
)
from accounts.tasks import process_referral_payouts
from common.models import SiteSettings

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def referrer(django_user_model: type[RevelUser]) -> RevelUser:
    """A referrer with Stripe Connect fully enabled."""
    return django_user_model.objects.create_user(
        username="payout_referrer@example.com",
        email="payout_referrer@example.com",
        password="pass",
        first_name="Payout",
        last_name="Referrer",
        preferred_name="Payout Referrer",
        stripe_account_id="acct_payout_001",
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )


@pytest.fixture
def referred_user(django_user_model: type[RevelUser]) -> RevelUser:
    """The user who was referred."""
    return django_user_model.objects.create_user(
        username="payout_referred@example.com",
        email="payout_referred@example.com",
        password="pass",
    )


@pytest.fixture
def referral_code(referrer: RevelUser) -> ReferralCode:
    """Active referral code for the referrer."""
    return ReferralCode.objects.create(user=referrer, code="PAYREF01")


@pytest.fixture
def referral(referral_code: ReferralCode, referred_user: RevelUser) -> Referral:
    """A referral link between referrer and referred user."""
    return Referral.objects.create(
        referral_code=referral_code,
        referred_user=referred_user,
        revenue_share_percent=Decimal("15.00"),
    )


@pytest.fixture
def billing_profile(referrer: RevelUser) -> UserBillingProfile:
    """Billing profile with self-billing agreed (B2C for simplicity)."""
    return UserBillingProfile.objects.create(
        user=referrer,
        billing_name="Payout Referrer",
        vat_id="",
        vat_country_code="AT",
        vat_id_validated=False,
        billing_address="Hauptstr. 1, 1010 Wien",
        billing_email="billing@payout-referrer.at",
        self_billing_agreed=True,
    )


@pytest.fixture
def site_settings() -> SiteSettings:
    """Populate SiteSettings singleton with platform business details."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel GmbH"
    site.platform_business_address = "Mariahilfer Str. 10, 1060 Wien, Austria"
    site.platform_vat_id = "ATU12345678"
    site.platform_vat_country = "AT"
    site.platform_vat_rate = Decimal("20.00")
    site.platform_invoice_bcc_email = "accounting@revel.at"
    site.save()
    return site


@pytest.fixture
def calculated_payout(referral: Referral) -> ReferralPayout:
    """A payout in CALCULATED status ready for processing."""
    return ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("15.00"),
        currency="EUR",
        status=ReferralPayout.Status.CALCULATED,
    )


def _mock_stripe_transfer() -> MagicMock:
    """Create a mock Stripe Transfer response."""
    transfer = MagicMock()
    transfer.id = "tr_test_123"
    return transfer


# ---------------------------------------------------------------------------
# Pre-flight skip scenarios
# ---------------------------------------------------------------------------


class TestPayoutPreflightChecks:
    """Test that payouts are skipped when pre-flight checks fail."""

    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    @patch("accounts.tasks.stripe.Transfer.create")
    def test_skips_when_stripe_charges_not_enabled(
        self,
        mock_transfer: MagicMock,
        mock_gen_statement: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Payout is skipped when referrer's Stripe charges are not enabled."""
        # Arrange
        referrer.stripe_charges_enabled = False
        referrer.save(update_fields=["stripe_charges_enabled"])

        # Act
        stats = process_referral_payouts()

        # Assert
        assert stats["skipped"] == 1
        assert stats["paid"] == 0
        assert stats["failed"] == 0
        mock_transfer.assert_not_called()
        mock_gen_statement.assert_not_called()

    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    @patch("accounts.tasks.stripe.Transfer.create")
    def test_skips_when_no_billing_profile(
        self,
        mock_transfer: MagicMock,
        mock_gen_statement: MagicMock,
        calculated_payout: ReferralPayout,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Payout is skipped when referrer has no billing profile at all."""
        # No billing_profile fixture injected -> no billing profile exists

        # Act
        stats = process_referral_payouts()

        # Assert
        assert stats["skipped"] == 1
        assert stats["paid"] == 0
        mock_transfer.assert_not_called()
        mock_gen_statement.assert_not_called()

    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    @patch("accounts.tasks.stripe.Transfer.create")
    def test_skips_when_self_billing_not_agreed(
        self,
        mock_transfer: MagicMock,
        mock_gen_statement: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Payout is skipped when referrer has not agreed to self-billing."""
        # Arrange
        billing_profile.self_billing_agreed = False
        billing_profile.save(update_fields=["self_billing_agreed"])

        # Act
        stats = process_referral_payouts()

        # Assert
        assert stats["skipped"] == 1
        assert stats["paid"] == 0
        mock_transfer.assert_not_called()
        mock_gen_statement.assert_not_called()

    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    @patch("accounts.tasks.stripe.Transfer.create")
    def test_only_processes_calculated_status(
        self,
        mock_transfer: MagicMock,
        mock_gen_statement: MagicMock,
        referral: Referral,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Only payouts with status CALCULATED are processed; other statuses are ignored."""
        # Arrange: create payouts in non-CALCULATED states (different period_start to avoid unique constraint)
        for i, status in enumerate(
            [ReferralPayout.Status.PAID, ReferralPayout.Status.FAILED, ReferralPayout.Status.PENDING]
        ):
            ReferralPayout.objects.create(
                referral=referral,
                period_start=date(2025, i + 1, 1),
                period_end=date(2025, i + 1, 28),
                net_platform_fees=Decimal("50.00"),
                payout_amount=Decimal("7.50"),
                currency="EUR",
                status=status,
            )

        # Act
        stats = process_referral_payouts()

        # Assert
        assert stats["paid"] == 0
        assert stats["skipped"] == 0
        assert stats["failed"] == 0
        mock_transfer.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path: successful payout
# ---------------------------------------------------------------------------


class TestPayoutHappyPath:
    """Test the full happy-path payout flow."""

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_creates_stripe_transfer_with_correct_params(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Stripe Transfer.create is called with correct amount, currency, and destination."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        mock_transfer_create.assert_called_once_with(
            amount=1500,  # 15.00 * 100 = 1500 cents
            currency="eur",
            destination="acct_payout_001",
            transfer_group=f"referral-payout-{calculated_payout.id}",
        )

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_updates_payout_status_to_paid(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """After successful Stripe transfer, payout status changes to PAID."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.return_value = _mock_stripe_transfer()

        stats = process_referral_payouts()

        calculated_payout.refresh_from_db()
        assert calculated_payout.status == ReferralPayout.Status.PAID
        assert stats["paid"] == 1
        assert stats["failed"] == 0
        assert stats["skipped"] == 0

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_stores_stripe_transfer_id(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """The Stripe transfer ID is persisted on the payout record."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        calculated_payout.refresh_from_db()
        assert calculated_payout.stripe_transfer_id == "tr_test_123"

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_generates_statement_before_transfer(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """The payout statement is generated before the Stripe transfer."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        mock_gen_statement.assert_called_once_with(calculated_payout)

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_sends_email_after_successful_transfer(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Email is dispatched after a successful payout transfer."""
        statement_mock = MagicMock(spec=ReferralPayoutStatement)
        mock_gen_statement.return_value = statement_mock
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        mock_send_email.assert_called_once_with(calculated_payout, statement_mock, referrer)


# ---------------------------------------------------------------------------
# Stripe error handling
# ---------------------------------------------------------------------------


class TestPayoutStripeError:
    """Test error handling when Stripe Transfer fails."""

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_updates_status_to_failed_on_stripe_error(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """When Stripe returns an error, the payout status is set to FAILED."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.side_effect = stripe.error.StripeError("Insufficient funds")

        with pytest.raises(stripe.error.StripeError):
            process_referral_payouts()

        calculated_payout.refresh_from_db()
        assert calculated_payout.status == ReferralPayout.Status.FAILED

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_propagates_stripe_exception_for_celery_retry(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """The StripeError is re-raised so Celery can retry the entire task."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.side_effect = stripe.error.StripeError("Connection error")

        with pytest.raises(stripe.error.StripeError, match="Connection error"):
            process_referral_payouts()

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_no_email_sent_on_stripe_failure(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Email is NOT sent when the Stripe transfer fails."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.side_effect = stripe.error.StripeError("Error")

        with pytest.raises(stripe.error.StripeError):
            process_referral_payouts()

        mock_send_email.assert_not_called()

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_stripe_transfer_id_not_set_on_failure(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """When Stripe fails, the transfer ID is not set on the payout."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.side_effect = stripe.error.StripeError("Error")

        with pytest.raises(stripe.error.StripeError):
            process_referral_payouts()

        calculated_payout.refresh_from_db()
        assert calculated_payout.stripe_transfer_id == ""


# ---------------------------------------------------------------------------
# Email dispatch
# ---------------------------------------------------------------------------


class TestPayoutEmailDispatch:
    """Test the email dispatch for payout statements."""

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_sent_with_correct_from_email_and_reply_to(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """Email uses DEFAULT_BILLING_EMAIL as from_email and DEFAULT_REPLY_TO_EMAIL as reply_to."""
        from django.conf import settings

        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["from_email"] == settings.DEFAULT_BILLING_EMAIL
        assert call_kwargs["reply_to"] == [settings.DEFAULT_REPLY_TO_EMAIL]

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_recipient_is_billing_email_when_set(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """When the billing profile has a billing_email, it is used as the recipient."""
        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["to"] == "billing@payout-referrer.at"

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_recipient_falls_back_to_user_email(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """When billing_email is empty, falls back to the referrer's user email."""
        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()
        billing_profile.billing_email = ""
        billing_profile.save(update_fields=["billing_email"])

        process_referral_payouts()

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["to"] == referrer.email

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_includes_bcc_from_site_settings(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Email BCC includes the platform_invoice_bcc_email from SiteSettings."""
        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["bcc"] == ["accounting@revel.at"]

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_no_bcc_when_site_settings_empty(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """When platform_invoice_bcc_email is empty, BCC list is empty."""
        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()
        site_settings.platform_invoice_bcc_email = ""
        site_settings.save()

        process_referral_payouts()

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["bcc"] == []

    @patch("accounts.tasks.send_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("common.service.invoice_utils.HTML")
    def test_email_includes_pdf_attachment(
        self,
        mock_html_cls: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Email includes the statement PDF as an attachment."""
        mock_html_cls.return_value.write_pdf.return_value = None
        mock_transfer_create.return_value = _mock_stripe_transfer()

        process_referral_payouts()

        call_kwargs = mock_send_email.call_args.kwargs
        assert "attachment_storage_path" in call_kwargs
        assert call_kwargs["attachment_storage_path"] is not None
        assert call_kwargs["attachment_filename"].endswith(".pdf")


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------


class TestPayoutReturnStats:
    """Test the stats dict returned by the task."""

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_returns_correct_stats_on_success(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """Stats dict reflects the number of paid/failed/skipped payouts."""
        mock_gen_statement.return_value = MagicMock(spec=ReferralPayoutStatement)
        mock_transfer_create.return_value = _mock_stripe_transfer()

        stats = process_referral_payouts()

        assert stats == {"paid": 1, "failed": 0, "skipped": 0}

    @patch("accounts.tasks._send_payout_statement_email")
    @patch("accounts.tasks.stripe.Transfer.create")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_empty_stats_when_no_calculated_payouts(
        self,
        mock_gen_statement: MagicMock,
        mock_transfer_create: MagicMock,
        mock_send_email: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """When there are no CALCULATED payouts, stats are all zeros."""
        stats = process_referral_payouts()

        assert stats == {"paid": 0, "failed": 0, "skipped": 0}
        mock_gen_statement.assert_not_called()
        mock_transfer_create.assert_not_called()
