"""Referral-aware account deletion (issue #796).

Before the fix, ``ReferralCode.user``, ``Referral.referrer`` and
``Referral.referred_user`` were all ``PROTECT``, so ``delete_user_account`` raised
``ProtectedError`` for anyone who held a code, referred somebody, or merely signed
up with a code — a silent GDPR Art. 17 failure (the endpoint had already returned
200 and burned the token).

These tests cover the repro, the force-confirmation contract, and the cleanup
service's retention rules.
"""

import typing as t
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import schema
from accounts.exceptions import ReferralForfeitureConfirmationRequiredError, ReferralPayoutInFlightError
from accounts.jwt import create_token
from accounts.models import (
    EmailVerificationReminderTracking,
    Referral,
    ReferralCode,
    ReferralPayout,
    ReferralPayoutStatement,
    RevelUser,
    UserBillingProfile,
)
from accounts.service import account as account_service
from accounts.service.referral_cleanup import assess_referral_forfeiture, cleanup_referral_data
from accounts.tasks import delete_old_inactive_accounts, delete_user_account
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

_Status = ReferralPayout.ReferralPayoutStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def referrer(django_user_model: type[RevelUser]) -> RevelUser:
    """The user who owns the referral code (user "A" in the issue)."""
    return django_user_model.objects.create_user(
        username="del_referrer@example.com",
        email="del_referrer@example.com",
        password="pass",
        first_name="Del",
        last_name="Referrer",
        stripe_account_id="acct_del_001",
        stripe_charges_enabled=True,
    )


@pytest.fixture
def referred(django_user_model: type[RevelUser]) -> RevelUser:
    """The user who signed up with the code (user "B" in the issue)."""
    return django_user_model.objects.create_user(
        username="del_referred@example.com",
        email="del_referred@example.com",
        password="pass",
    )


@pytest.fixture
def referral_code(referrer: RevelUser) -> ReferralCode:
    return ReferralCode.objects.create(user=referrer, code="DELREF01")


@pytest.fixture
def referral(referral_code: ReferralCode, referred: RevelUser) -> Referral:
    return Referral.objects.create(
        referral_code=referral_code,
        referred_user=referred,
        revenue_share_percent=Decimal("15.00"),
    )


@pytest.fixture
def billing_profile(referrer: RevelUser) -> UserBillingProfile:
    return UserBillingProfile.objects.create(
        user=referrer,
        billing_name="Del Referrer",
        vat_country_code="AT",
        billing_address="Hauptstr. 1, 1010 Wien",
        self_billing_agreed=True,
    )


@pytest.fixture
def site_settings() -> SiteSettings:
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel GmbH"
    site.platform_business_address = "Mariahilfer Str. 10, 1060 Wien, Austria"
    site.platform_vat_id = "ATU12345678"
    site.platform_vat_country = "AT"
    site.platform_vat_rate = Decimal("20.00")
    site.save()
    return site


def _payout(referral: Referral, status: str, amount: str, month: int = 1) -> ReferralPayout:
    """Create a payout for ``referral`` in a distinct period."""
    return ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        net_platform_fees=Decimal(amount) * 10,
        payout_amount=Decimal(amount),
        currency="EUR",
        status=status,
    )


def _statement(payout: ReferralPayout, number: str) -> ReferralPayoutStatement:
    """Attach a minimal issued statement to ``payout``."""
    return ReferralPayoutStatement.objects.create(
        payout=payout,
        document_type=ReferralPayoutStatement.DocumentType.PAYOUT_STATEMENT,
        document_number=number,
        amount_gross=payout.payout_amount,
        amount_net=payout.payout_amount,
        amount_vat=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
        currency=payout.currency,
        referrer_name="Del Referrer",
        platform_business_name="Revel GmbH",
        platform_business_address="Mariahilfer Str. 10",
        platform_vat_id="ATU12345678",
        issued_at=timezone.now(),
    )


def _deletion_token(user: RevelUser) -> str:
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Repro: the referred user (B) could not be deleted at all
# ---------------------------------------------------------------------------


# transaction=True: confirm_account_deletion dispatches delete_user_account via transaction.on_commit.
@pytest.mark.django_db(transaction=True)
def test_referred_user_deletion_is_silent_and_keeps_referrer_history(
    referrer: RevelUser, referred: RevelUser, referral: Referral
) -> None:
    """B signed up with A's code: deletion needs no force and leaves A's payout history intact."""
    paid = _payout(referral, _Status.PAID, "20.00")
    referred_id = referred.id

    account_service.confirm_account_deletion(_deletion_token(referred))

    assert not RevelUser.objects.filter(id=referred_id).exists()
    # The referral row carries A's payout, so it survives with referred_user nulled.
    referral.refresh_from_db()
    assert referral.referred_user_id is None
    paid.refresh_from_db()
    assert paid.referral_id == referral.id
    assert RevelUser.objects.filter(id=referrer.id).exists()


def test_referred_user_without_payouts_drops_the_referral_row(referred: RevelUser, referral: Referral) -> None:
    """With no payout history there is nothing to anonymize — the row goes."""
    cleanup_referral_data(referred)
    referred.delete()

    assert not Referral.objects.filter(id=referral.id).exists()


def test_referred_only_user_needs_no_force(referred: RevelUser, referral: Referral) -> None:
    assert assess_referral_forfeiture(referred) is None


def test_referrer_with_unused_code_needs_no_force(referrer: RevelUser, referral_code: ReferralCode) -> None:
    """A code nobody used costs its owner nothing — clean up silently."""
    assert assess_referral_forfeiture(referrer) is None

    cleanup_referral_data(referrer)
    referrer.delete()

    assert not ReferralCode.objects.filter(id=referral_code.id).exists()


# ---------------------------------------------------------------------------
# Force-confirmation contract
# ---------------------------------------------------------------------------


def test_assessment_excludes_rolled_over_and_paid_from_the_total(referrer: RevelUser, referral: Referral) -> None:
    """CALCULATED + FAILED are forfeited; ROLLED_OVER is already folded into a later row."""
    _payout(referral, _Status.CALCULATED, "10.00", month=1)
    _payout(referral, _Status.FAILED, "12.00", month=2)
    _payout(referral, _Status.ROLLED_OVER, "5.00", month=3)
    _payout(referral, _Status.PAID, "20.00", month=4)

    summary = assess_referral_forfeiture(referrer)

    assert summary is not None
    assert summary.unpaid_count == 2
    assert summary.unpaid_total == Decimal("22.00")
    assert summary.failed_total == Decimal("12.00")
    assert summary.currency == "EUR"
    # The rolled-over row is still itemized for the admin trail (it is deleted too).
    assert {i.status for i in summary.items} == {_Status.CALCULATED, _Status.FAILED, _Status.ROLLED_OVER}


def test_confirm_raises_forfeiture_error_without_force(referrer: RevelUser, referral: Referral) -> None:
    _payout(referral, _Status.CALCULATED, "10.00")

    with pytest.raises(ReferralForfeitureConfirmationRequiredError) as exc_info:
        account_service.confirm_account_deletion(_deletion_token(referrer))

    assert exc_info.value.summary.unpaid_total == Decimal("10.00")
    assert RevelUser.objects.filter(id=referrer.id).exists()


def test_confirm_endpoint_returns_409_payload_and_keeps_the_token(
    client: Client, referrer: RevelUser, referral: Referral
) -> None:
    """The 409 is machine-readable and the token survives for the forced retry."""
    _payout(referral, _Status.CALCULATED, "10.00", month=1)
    _payout(referral, _Status.FAILED, "12.00", month=2)
    _payout(referral, _Status.ROLLED_OVER, "5.00", month=3)
    token = _deletion_token(referrer)
    url = reverse("api:delete-account-confirm")

    response = client.post(url, data=orjson.dumps({"token": token}), content_type="application/json")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "referral_forfeiture_confirmation_required"
    assert body["unpaid_count"] == 2
    assert body["unpaid_total"] == "22.00"
    assert body["failed_total"] == "12.00"
    assert body["currency"] == "EUR"
    assert "22.00" in body["detail"]

    # Token not burned: the same token is still accepted on the forced re-submit.
    forced = client.post(url, data=orjson.dumps({"token": token, "force": True}), content_type="application/json")
    assert forced.status_code == 200


# transaction=True: the forced confirm dispatches delete_user_account via transaction.on_commit.
@pytest.mark.django_db(transaction=True)
def test_force_deletes_referrer_and_retains_paid_history(
    referrer: RevelUser, referral: Referral, superuser: RevelUser
) -> None:
    """Force path: unpaid rows are forfeited, PAID rows + statements are retained detached."""
    calculated = _payout(referral, _Status.CALCULATED, "10.00", month=1)
    paid = _payout(referral, _Status.PAID, "20.00", month=2)
    statement = _statement(paid, "RVL-RP-2026-000001")
    referrer_id = referrer.id

    account_service.confirm_account_deletion(_deletion_token(referrer), force=True)

    assert not RevelUser.objects.filter(id=referrer_id).exists()
    assert not ReferralPayout.objects.filter(id=calculated.id).exists()
    assert not Referral.objects.filter(id=referral.id).exists()
    assert not ReferralCode.objects.filter(user_id=referrer_id).exists()

    paid.refresh_from_db()
    assert paid.referral_id is None
    assert ReferralPayoutStatement.objects.filter(id=statement.id).exists()


# ---------------------------------------------------------------------------
# Cleanup service internals
# ---------------------------------------------------------------------------


def test_statements_are_backfilled_before_the_user_disappears(
    referrer: RevelUser, referral: Referral, billing_profile: UserBillingProfile, site_settings: SiteSettings
) -> None:
    """A PAID payout with no statement gets one synchronously — the profile CASCADEs away."""
    paid = _payout(referral, _Status.PAID, "20.00")
    assert not hasattr(paid, "statement")

    cleanup_referral_data(referrer)

    paid.refresh_from_db()
    assert ReferralPayoutStatement.objects.filter(payout=paid).exists()
    assert paid.referral_id is None


def test_backfill_survives_a_missing_billing_profile(referrer: RevelUser, referral: Referral) -> None:
    """A billing profile removed after the transfer must not block an Art. 17 erasure."""
    paid = _payout(referral, _Status.PAID, "20.00")

    cleanup_referral_data(referrer)

    paid.refresh_from_db()
    assert paid.referral_id is None
    assert not ReferralPayoutStatement.objects.filter(payout=paid).exists()


def test_pending_payout_defers_cleanup(referrer: RevelUser, referral: Referral) -> None:
    """A Stripe transfer may be in flight — never race it."""
    _payout(referral, _Status.PENDING, "10.00")

    with pytest.raises(ReferralPayoutInFlightError):
        cleanup_referral_data(referrer)

    assert Referral.objects.filter(id=referral.id).exists()


def test_delete_user_account_task_defers_on_pending_payout(referrer: RevelUser, referral: Referral) -> None:
    """The task retries rather than deleting a user mid-transfer."""
    _payout(referral, _Status.PENDING, "10.00")

    with pytest.raises(ReferralPayoutInFlightError):
        delete_user_account(str(referrer.id))

    assert RevelUser.objects.filter(id=referrer.id).exists()


def test_cleanup_is_idempotent(referrer: RevelUser, referral: Referral) -> None:
    _payout(referral, _Status.CALCULATED, "10.00")

    assert cleanup_referral_data(referrer) is not None
    assert cleanup_referral_data(referrer) is None

    referrer.delete()
    assert not RevelUser.objects.filter(id=referrer.id).exists()


def test_admins_are_notified_with_the_itemization(
    referrer: RevelUser, referral: Referral, superuser: RevelUser
) -> None:
    calculated = _payout(referral, _Status.CALCULATED, "10.00", month=1)
    failed = _payout(referral, _Status.FAILED, "12.00", month=2)

    with patch("accounts.service.referral_cleanup.send_email") as mock_send:
        cleanup_referral_data(referrer)

    mock_send.assert_called_once()
    body = mock_send.call_args.kwargs["body"]
    assert str(calculated.id) in body
    assert str(failed.id) in body
    assert "12.00" in body


def test_no_admin_notification_when_nothing_is_forfeited(referrer: RevelUser, referral: Referral) -> None:
    _payout(referral, _Status.PAID, "20.00")

    with patch("accounts.service.referral_cleanup.send_email") as mock_send:
        assert cleanup_referral_data(referrer) is None

    mock_send.assert_not_called()


def test_forfeiture_notification_without_admins_is_logged_not_raised(referrer: RevelUser, referral: Referral) -> None:
    """No staff configured must not turn an erasure into a crash."""
    RevelUser.objects.filter(is_staff=True).update(is_staff=False, is_superuser=False)
    _payout(referral, _Status.CALCULATED, "10.00")

    with patch("accounts.service.referral_cleanup.send_email") as mock_send:
        summary = cleanup_referral_data(referrer)

    assert summary is not None
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Unverified-account sweep isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sweep_isolates_per_user_failures(django_user_model: type[RevelUser]) -> None:
    """One undeletable account no longer strands everyone behind it in the loop."""
    users = []
    for i in range(2):
        u = django_user_model.objects.create_user(
            username=f"sweep{i}@example.com", email=f"sweep{i}@example.com", password="pass"
        )
        u.is_active = False
        u.email_verified = False
        u.save(update_fields=["is_active", "email_verified"])
        EmailVerificationReminderTracking.objects.create(
            user=u, deactivation_email_sent_at=timezone.now() - timedelta(days=61)
        )
        users.append(u)

    original = cleanup_referral_data

    def _boom(user: RevelUser) -> t.Any:
        if user.id == users[0].id:
            raise RuntimeError("boom")
        return original(user)

    with patch("accounts.tasks.verification_reminders.cleanup_referral_data", side_effect=_boom):
        result = delete_old_inactive_accounts()

    assert result["count"] == 1
    assert RevelUser.objects.filter(id=users[0].id).exists()
    assert not RevelUser.objects.filter(id=users[1].id).exists()


@pytest.mark.django_db
def test_sweep_cleans_up_a_referred_unverified_user(
    django_user_model: type[RevelUser], referral_code: ReferralCode
) -> None:
    """A never-verified user who signed up with a code used to abort the whole sweep."""
    u = django_user_model.objects.create_user(
        username="sweep_ref@example.com", email="sweep_ref@example.com", password="pass"
    )
    u.is_active = False
    u.email_verified = False
    u.save(update_fields=["is_active", "email_verified"])
    Referral.objects.create(referral_code=referral_code, referred_user=u)
    EmailVerificationReminderTracking.objects.create(
        user=u, deactivation_email_sent_at=timezone.now() - timedelta(days=61)
    )

    result = delete_old_inactive_accounts()

    assert result["count"] == 1
    assert not RevelUser.objects.filter(id=u.id).exists()


# ---------------------------------------------------------------------------
# Detached-payout hardening
# ---------------------------------------------------------------------------


def test_referral_str_is_none_safe(referral: Referral) -> None:
    referral.referred_user = None
    referral.save(update_fields=["referred_user"])

    assert "(deleted user)" in str(referral)


def test_payout_str_is_none_safe(referral: Referral) -> None:
    payout = _payout(referral, _Status.PAID, "20.00")
    payout.referral = None

    assert "(detached referral)" in str(payout)


def test_generate_payout_statement_rejects_a_detached_payout(referral: Referral) -> None:
    from accounts.service.payout_statement_service import generate_payout_statement

    payout = _payout(referral, _Status.PAID, "20.00")
    ReferralPayout.objects.filter(id=payout.id).update(referral=None)
    payout.refresh_from_db()

    with pytest.raises(ValueError, match="detached"):
        generate_payout_statement(payout)


def test_statement_task_skips_a_detached_payout(referral: Referral) -> None:
    from accounts.tasks.payouts import generate_and_send_payout_statement

    payout = _payout(referral, _Status.PAID, "20.00")
    ReferralPayout.objects.filter(id=payout.id).update(referral=None)

    with patch("accounts.tasks.payouts._send_payout_statement_email") as mock_send:
        generate_and_send_payout_statement(str(payout.id))

    mock_send.assert_not_called()


def test_statement_backstop_skips_detached_payouts(referral: Referral) -> None:
    """A detached PAID payout has no referrer to email — it must not be re-dispatched forever."""
    from accounts.tasks.payouts import _redispatch_missing_statements

    payout = _payout(referral, _Status.PAID, "20.00")
    ReferralPayout.objects.filter(id=payout.id).update(referral=None)

    with patch("accounts.tasks.payouts.generate_and_send_payout_statement.delay") as mock_delay:
        _redispatch_missing_statements()

    mock_delay.assert_not_called()


def test_monthly_calculation_skips_detached_referrals(referral: Referral) -> None:
    """A referral whose referred user left can never earn new fees."""
    from events.service.referral_payout_service import calculate_payouts_for_period

    Referral.objects.filter(id=referral.id).update(referred_user=None)
    rates = MagicMock(rates={"EUR": 1.0})

    with patch("events.service.referral_payout_service.get_latest_rates", return_value=rates):
        result = calculate_payouts_for_period(date(2026, 1, 1), date(2026, 1, 31))

    assert result == {"created": 0, "skipped": 0}


def test_delete_account_confirm_schema_defaults_to_no_force() -> None:
    assert schema.DeleteAccountConfirmSchema(token="x").force is False
