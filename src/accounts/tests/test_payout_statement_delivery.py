"""Tests for payout statement generation/delivery and the backstop sweep.

Split out of test_payout_task.py (1000-line limit). Covers:
- generate_and_send_payout_statement: the retryable per-payout statement task.
- The backstop sweep that re-dispatches PAID payouts missing a statement or a
  successful delivery (issues #611 / #616).
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
from accounts.tasks import generate_and_send_payout_statement, process_referral_payouts
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
        status=ReferralPayout.ReferralPayoutStatus.CALCULATED,
    )


def _mock_stripe_transfer() -> MagicMock:
    """Create a mock Stripe Transfer response."""
    transfer = MagicMock()
    transfer.id = "tr_test_123"
    return transfer


class TestGenerateAndSendPayoutStatementTask:
    """The retryable per-payout statement task delegates to generate + email."""

    @patch("accounts.tasks.payouts._send_payout_statement_email")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_generates_statement_and_sends_email(
        self,
        mock_gen_statement: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        referrer: RevelUser,
        site_settings: SiteSettings,
    ) -> None:
        """The task loads the PAID payout by id, generates the statement, and emails it to the referrer."""
        calculated_payout.status = ReferralPayout.ReferralPayoutStatus.PAID
        calculated_payout.save(update_fields=["status"])
        statement_mock = MagicMock(spec=ReferralPayoutStatement)
        mock_gen_statement.return_value = statement_mock

        generate_and_send_payout_statement(str(calculated_payout.id))

        mock_gen_statement.assert_called_once()
        assert mock_gen_statement.call_args.args[0] == calculated_payout
        mock_send_email.assert_called_once_with(calculated_payout, statement_mock, referrer)

    @patch("accounts.tasks.payouts._send_payout_statement_email")
    @patch("accounts.service.payout_statement_service.generate_payout_statement")
    def test_skips_non_paid_payout(
        self,
        mock_gen_statement: MagicMock,
        mock_send_email: MagicMock,
        calculated_payout: ReferralPayout,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """The task is a no-op for a non-PAID payout — no statement is generated, no email is sent."""
        # calculated_payout is still CALCULATED — generating a statement here would
        # issue a financial document for an unpaid payout.
        generate_and_send_payout_statement(str(calculated_payout.id))

        mock_gen_statement.assert_not_called()
        mock_send_email.assert_not_called()


class TestPayoutStatementBackstopSweep:
    """PAID payouts missing a statement are re-dispatched at the start of each run (issue #611)."""

    @staticmethod
    def _paid_payout(referral: Referral, month: int) -> ReferralPayout:
        return ReferralPayout.objects.create(
            referral=referral,
            period_start=date(2026, month, 1),
            period_end=date(2026, month, 28),
            net_platform_fees=Decimal("100.00"),
            payout_amount=Decimal("15.00"),
            currency="EUR",
            status=ReferralPayout.ReferralPayoutStatus.PAID,
        )

    @patch("accounts.tasks.payouts.generate_and_send_payout_statement.delay")
    @patch("accounts.tasks.payouts.stripe.Transfer.create")
    def test_redispatches_paid_payout_missing_statement(
        self,
        mock_transfer_create: MagicMock,
        mock_delay: MagicMock,
        referral: Referral,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """A PAID payout with no statement is re-dispatched even though reruns never re-scan PAID rows."""
        paid_payout = self._paid_payout(referral, month=1)

        process_referral_payouts()

        mock_delay.assert_called_once_with(str(paid_payout.id))
        mock_transfer_create.assert_not_called()  # no CALCULATED payouts to transfer

    @staticmethod
    def _statement(payout: ReferralPayout, *, number: str, delivered: bool) -> ReferralPayoutStatement:
        return ReferralPayoutStatement.objects.create(
            payout=payout,
            document_type=ReferralPayoutStatement.DocumentType.PAYOUT_STATEMENT,
            document_number=number,
            amount_gross=Decimal("15.00"),
            amount_net=Decimal("15.00"),
            amount_vat=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            referrer_name="Payout Referrer",
            platform_business_name="Revel GmbH",
            platform_business_address="Mariahilfer Str. 10, 1060 Wien, Austria",
            platform_vat_id="ATU12345678",
            email_sent_at=timezone.now() if delivered else None,
        )

    @patch("accounts.tasks.payouts.generate_and_send_payout_statement.delay")
    @patch("accounts.tasks.payouts.stripe.Transfer.create")
    def test_does_not_redispatch_when_statement_delivered(
        self,
        mock_transfer_create: MagicMock,
        mock_delay: MagicMock,
        referral: Referral,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """A PAID payout whose statement was already delivered (email_sent_at set) is left alone."""
        paid_payout = self._paid_payout(referral, month=1)
        self._statement(paid_payout, number="RVL-RP-2026-000001", delivered=True)

        process_referral_payouts()

        mock_delay.assert_not_called()

    @patch("accounts.tasks.payouts.generate_and_send_payout_statement.delay")
    @patch("accounts.tasks.payouts.stripe.Transfer.create")
    def test_redispatches_paid_payout_with_undelivered_statement(
        self,
        mock_transfer_create: MagicMock,
        mock_delay: MagicMock,
        referral: Referral,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """A statement that exists but whose email never delivered (email_sent_at null) is re-dispatched (#616)."""
        paid_payout = self._paid_payout(referral, month=1)
        self._statement(paid_payout, number="RVL-RP-2026-000001", delivered=False)

        process_referral_payouts()

        mock_delay.assert_called_once_with(str(paid_payout.id))

    @patch("accounts.tasks.payouts.generate_and_send_payout_statement.delay")
    @patch("accounts.tasks.payouts.stripe.Transfer.create")
    def test_sweep_dispatch_failure_does_not_halt_run(
        self,
        mock_transfer_create: MagicMock,
        mock_delay: MagicMock,
        calculated_payout: ReferralPayout,
        referral: Referral,
        billing_profile: UserBillingProfile,
        site_settings: SiteSettings,
    ) -> None:
        """A broker outage during the sweep is isolated: the CALCULATED batch still runs and pays out."""
        self._paid_payout(referral, month=2)  # missing-statement row to sweep
        mock_transfer_create.return_value = _mock_stripe_transfer()
        mock_delay.side_effect = RuntimeError("broker unavailable")

        stats = process_referral_payouts()

        assert stats["paid"] == 1
        calculated_payout.refresh_from_db()
        assert calculated_payout.status == ReferralPayout.ReferralPayoutStatus.PAID
