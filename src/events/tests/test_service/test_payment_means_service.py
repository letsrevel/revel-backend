"""Payment means snapshot from the Stripe charge (BT-81/BT-82, BG-18; #1234)."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
import stripe
from structlog.testing import capture_logs

from events.models import Organization
from events.service.payment_means_service import PaymentMeans, fetch_payment_means, payment_means_from_charge

pytestmark = pytest.mark.real_payment_means

FALLBACK = PaymentMeans("ZZZ", "Paid online via Stripe", "")
RETRIEVE = "events.service.payment_means_service.stripe.PaymentIntent.retrieve"


def _charge(method_type: str, **extra: t.Any) -> dict[str, t.Any]:
    return {"payment_method_details": {"type": method_type, **extra}}


def test_card_charge_is_48_with_last4() -> None:
    assert payment_means_from_charge(_charge("card", card={"last4": "4242"})) == PaymentMeans(
        "48", "Paid by card", "4242"
    )


def test_card_without_last4_is_48_without_digits() -> None:
    assert payment_means_from_charge(_charge("card", card={})) == PaymentMeans("48", "Paid by card", "")


def test_sepa_is_zzz_with_label() -> None:
    assert payment_means_from_charge(_charge("sepa_debit")) == PaymentMeans(
        "ZZZ", "Paid online via Stripe (SEPA Direct Debit)", ""
    )


def test_unknown_type_label() -> None:
    assert payment_means_from_charge(_charge("us_bank_account")).label == "Paid online via Stripe (us bank account)"


@pytest.mark.parametrize("charge", [None, {}, {"payment_method_details": None}, {"payment_method_details": {}}])
def test_missing_charge_falls_back(charge: dict[str, t.Any] | None) -> None:
    assert payment_means_from_charge(charge) == FALLBACK


def test_fetch_without_intent_skips_stripe(organization: Organization) -> None:
    with patch(RETRIEVE) as retrieve:
        assert fetch_payment_means(organization, "") == FALLBACK
    retrieve.assert_not_called()


def test_fetch_stripe_error_falls_back_and_logs_ids_only(organization: Organization) -> None:
    with patch(RETRIEVE, side_effect=stripe.APIConnectionError("boom")), capture_logs() as logs:
        assert fetch_payment_means(organization, "pi_1") == FALLBACK
    events = [log for log in logs if log["event"] == "payment_means_lookup_failed"]
    assert len(events) == 1
    assert events[0]["org_id"] == str(organization.id)
    assert set(events[0]) <= {"event", "log_level", "org_id"}


def test_fetch_passes_connected_account(organization: Organization) -> None:
    organization.stripe_account_id = "acct_123"
    pi = MagicMock()
    pi.get.return_value = _charge("card", card={"last4": "4242"})
    with patch(RETRIEVE, return_value=pi) as retrieve:
        result = fetch_payment_means(organization, "pi_1")
    assert result == PaymentMeans("48", "Paid by card", "4242")
    retrieve.assert_called_once_with("pi_1", expand=["latest_charge"], stripe_account="acct_123")


def test_fetch_omits_account_when_it_is_the_platform_account(organization: Organization, settings: t.Any) -> None:
    settings.STRIPE_ACCOUNT = "acct_platform"
    organization.stripe_account_id = "acct_platform"
    pi = MagicMock()
    pi.get.return_value = _charge("card", card={"last4": "4242"})
    with patch(RETRIEVE, return_value=pi) as retrieve:
        fetch_payment_means(organization, "pi_1")
    retrieve.assert_called_once_with("pi_1", expand=["latest_charge"])


def test_fetch_omits_blank_account_and_unexpanded_charge_falls_back(organization: Organization) -> None:
    organization.stripe_account_id = ""
    pi = MagicMock()
    pi.get.return_value = "ch_123"  # not expanded
    with patch(RETRIEVE, return_value=pi) as retrieve:
        assert fetch_payment_means(organization, "pi_1") == FALLBACK
    retrieve.assert_called_once_with("pi_1", expand=["latest_charge"])


def test_stripe_link_label() -> None:
    assert payment_means_from_charge(_charge("link")).label == "Paid online via Stripe (Link)"
