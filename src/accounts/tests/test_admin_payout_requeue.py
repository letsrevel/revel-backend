"""Tests for the ReferralPayout admin's requeue action (issue #797).

Covers the admin action (status filtering, message counts, permission gating),
the underlying service (audit log line), and the integration guarantee that a
requeued payout is picked up by the next ``process_referral_payouts`` scan.
"""

import typing as t
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages import constants as message_levels
from django.contrib.messages.storage.fallback import FallbackStorage
from structlog.testing import capture_logs

from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser, UserBillingProfile
from accounts.tasks import process_referral_payouts

pytestmark = pytest.mark.django_db

_Status = ReferralPayout.ReferralPayoutStatus


def _admin() -> t.Any:
    return site._registry[ReferralPayout]


def _request_with_messages(rf: t.Any, request_user: RevelUser) -> t.Any:
    request = rf.post("/")
    request.user = request_user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _messages(request: t.Any) -> list[tuple[int, str]]:
    return [(m.level, m.message) for m in request._messages]


@pytest.fixture
def referrer(django_user_model: type[RevelUser]) -> RevelUser:
    """A referrer with Stripe Connect fully enabled."""
    return django_user_model.objects.create_user(
        username="requeue_referrer@example.com",
        email="requeue_referrer@example.com",
        password="pass",
        stripe_account_id="acct_requeue_001",
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )


@pytest.fixture
def referral(referrer: RevelUser, django_user_model: type[RevelUser]) -> Referral:
    """A referral link owned by ``referrer``."""
    referred = django_user_model.objects.create_user(
        username="requeue_referred@example.com",
        email="requeue_referred@example.com",
        password="pass",
    )
    code = ReferralCode.objects.create(user=referrer, code="REQUEUE1")
    return Referral.objects.create(
        referral_code=code,
        referred_user=referred,
        revenue_share_percent=Decimal("15.00"),
    )


def _payout(referral: Referral, status: str, month: int) -> ReferralPayout:
    return ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("15.00"),
        currency="EUR",
        status=status,
    )


@pytest.fixture
def failed_payout(referral: Referral) -> ReferralPayout:
    return _payout(referral, _Status.FAILED, month=1)


def test_requeue_flips_failed_payout_to_calculated(
    rf: t.Any, failed_payout: ReferralPayout, superuser: RevelUser
) -> None:
    """A FAILED payout goes back to CALCULATED so disbursement retries the transfer."""
    request = _request_with_messages(rf, superuser)

    _admin().requeue_failed_payouts(request, ReferralPayout.objects.filter(pk=failed_payout.pk))

    failed_payout.refresh_from_db()
    assert failed_payout.status == _Status.CALCULATED
    assert _messages(request) == [(message_levels.SUCCESS, "Requeued 1 failed payout.")]


def test_requeue_leaves_non_failed_rows_untouched(
    rf: t.Any, referral: Referral, failed_payout: ReferralPayout, superuser: RevelUser
) -> None:
    """Rows in any other status are skipped silently and reported as skipped."""
    paid = _payout(referral, _Status.PAID, month=2)
    calculated = _payout(referral, _Status.CALCULATED, month=3)
    pending = _payout(referral, _Status.PENDING, month=4)
    request = _request_with_messages(rf, superuser)

    _admin().requeue_failed_payouts(request, ReferralPayout.objects.all())

    for payout, expected in ((paid, _Status.PAID), (calculated, _Status.CALCULATED), (pending, _Status.PENDING)):
        payout.refresh_from_db()
        assert payout.status == expected
    failed_payout.refresh_from_db()
    assert failed_payout.status == _Status.CALCULATED
    assert _messages(request) == [
        (message_levels.SUCCESS, "Requeued 1 failed payout."),
        (message_levels.WARNING, "Skipped 3 payouts (not in failed status)."),
    ]


def test_requeue_reports_plural_counts(rf: t.Any, referral: Referral, superuser: RevelUser) -> None:
    """Both counts are pluralised from the real number of affected rows."""
    _payout(referral, _Status.FAILED, month=1)
    _payout(referral, _Status.FAILED, month=2)
    _payout(referral, _Status.PAID, month=3)
    request = _request_with_messages(rf, superuser)

    _admin().requeue_failed_payouts(request, ReferralPayout.objects.all())

    assert ReferralPayout.objects.filter(status=_Status.CALCULATED).count() == 2
    assert _messages(request) == [
        (message_levels.SUCCESS, "Requeued 2 failed payouts."),
        (message_levels.WARNING, "Skipped 1 payout (not in failed status)."),
    ]


def test_requeue_with_no_failed_rows_reports_only_skips(rf: t.Any, referral: Referral, superuser: RevelUser) -> None:
    """Selecting only healthy rows produces a skip warning and no success message."""
    _payout(referral, _Status.PAID, month=1)
    request = _request_with_messages(rf, superuser)

    _admin().requeue_failed_payouts(request, ReferralPayout.objects.all())

    assert _messages(request) == [(message_levels.WARNING, "Skipped 1 payout (not in failed status).")]


def test_requeue_logs_payout_requeued_with_actor(
    rf: t.Any, failed_payout: ReferralPayout, superuser: RevelUser
) -> None:
    """The audit line carries the payout id and who requeued it."""
    request = _request_with_messages(rf, superuser)

    with capture_logs() as logs:
        _admin().requeue_failed_payouts(request, ReferralPayout.objects.filter(pk=failed_payout.pk))

    (entry,) = [line for line in logs if line["event"] == "payout_requeued"]
    assert entry["payout_id"] == str(failed_payout.id)
    assert entry["actor_id"] == str(superuser.id)
    assert entry["actor_email"] == superuser.email


def test_requeue_action_requires_change_permission(rf: t.Any, staff_user: RevelUser) -> None:
    """A staff user without ``accounts.change_referralpayout`` cannot see the action."""
    assert _admin().has_requeue_permission(_request_with_messages(rf, staff_user)) is False


def test_requeue_action_available_to_superuser(rf: t.Any, superuser: RevelUser) -> None:
    """A superuser holds every permission, so the action is offered."""
    assert _admin().has_requeue_permission(_request_with_messages(rf, superuser)) is True


@patch("accounts.tasks.payouts.stripe.Transfer.create")
def test_requeued_payout_is_picked_up_by_disbursement(
    mock_transfer: MagicMock,
    rf: t.Any,
    failed_payout: ReferralPayout,
    referrer: RevelUser,
    superuser: RevelUser,
) -> None:
    """End to end: requeue makes the next process_referral_payouts run retry the transfer."""
    UserBillingProfile.objects.create(
        user=referrer,
        billing_name="Requeue Referrer",
        vat_id="",
        vat_country_code="AT",
        vat_id_validated=False,
        billing_address="Hauptstr. 1, 1010 Wien",
        billing_email="billing@requeue-referrer.at",
        self_billing_agreed=True,
    )
    transfer = MagicMock()
    transfer.id = "tr_requeued_001"
    mock_transfer.return_value = transfer

    # Before the requeue the disbursement scan ignores the FAILED row entirely.
    assert process_referral_payouts() == {"paid": 0, "failed": 0, "skipped": 0}
    mock_transfer.assert_not_called()

    _admin().requeue_failed_payouts(
        _request_with_messages(rf, superuser), ReferralPayout.objects.filter(pk=failed_payout.pk)
    )

    stats = process_referral_payouts()

    assert stats["paid"] == 1
    # The idempotency key is what makes a retry safe: a transfer that did reach
    # Stripe is returned rather than duplicated.
    assert mock_transfer.call_args.kwargs["idempotency_key"] == f"referral-payout-{failed_payout.id}"
    failed_payout.refresh_from_db()
    assert failed_payout.status == _Status.PAID
    assert failed_payout.stripe_transfer_id == "tr_requeued_001"
