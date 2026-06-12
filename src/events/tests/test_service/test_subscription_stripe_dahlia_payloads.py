"""Payload-shape tests for API versions >= 2025-03-31.basil (pinned: dahlia).

The pinned webhook endpoints render Subscription/Invoice payloads with:
- ``current_period_{start,end}`` on subscription items, not the Subscription;
- the subscription reference at ``invoice.parent.subscription_details``;
- ``invoice.payment_intent`` replaced by the ``payments`` list (not embedded
  in webhook payloads — requires an outbound expand);
- the confirmable secret at ``latest_invoice.confirmation_secret``.

The legacy top-level paths are covered by test_subscription_stripe_service.py;
these tests pin the modern paths so a regression can't silently drop every
subscription invoice event (M1 in the 2026-06-10 reassessment).
"""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_stripe_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    return organization


@pytest.fixture
def online_plan(stripe_org: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=stripe_org, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="dahlia_sub", email="dahlia@example.com", password="pass")


@pytest.fixture
def pending_subscription(online_plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=online_plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_subscription_id="sub_dahlia",
    )


def test_sync_reads_period_from_subscription_items(pending_subscription: MembershipSubscription) -> None:
    payload = {
        "id": "sub_dahlia",
        "status": "active",
        "cancel_at_period_end": False,
        # No top-level current_period_* — basil+ shape.
        "items": {
            "data": [
                {
                    "id": "si_x",
                    "current_period_start": 1_800_000_000,
                    "current_period_end": 1_800_000_000 + 30 * 86400,
                    "price": {"id": "price_test"},
                }
            ]
        },
    }
    result = subscription_stripe_service.sync_subscription_from_stripe(payload)
    assert result is not None
    result.refresh_from_db()
    assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert result.current_period_start is not None
    assert result.current_period_end is not None


def _dahlia_invoice(payments: dict[str, t.Any] | None) -> dict[str, t.Any]:
    invoice: dict[str, t.Any] = {
        "id": "in_dahlia",
        "amount_paid": 1000,
        "currency": "eur",
        # basil+ shape: no top-level "subscription" / "payment_intent".
        "parent": {"subscription_details": {"subscription": "sub_dahlia", "metadata": {}}},
        "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 30 * 86400}}]},
    }
    if payments is not None:
        invoice["payments"] = payments
    return invoice


def test_invoice_paid_resolves_subscription_via_parent_details(
    pending_subscription: MembershipSubscription,
) -> None:
    payments = {"data": [{"payment": {"type": "payment_intent", "payment_intent": "pi_dahlia"}}]}
    payment = subscription_stripe_service.record_stripe_payment_from_invoice(_dahlia_invoice(payments), succeeded=True)
    assert payment is not None
    assert payment.subscription_id == pending_subscription.pk
    assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
    assert payment.stripe_payment_intent_id == "pi_dahlia"
    pending_subscription.refresh_from_db()
    assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE


def test_invoice_without_embedded_payments_fetches_outbound(
    pending_subscription: MembershipSubscription,
) -> None:
    """Webhook payloads don't embed ``payments`` — resolve via Invoice.retrieve."""
    retrieved = {"payments": {"data": [{"payment": {"type": "payment_intent", "payment_intent": "pi_fetched"}}]}}
    with mock.patch(
        "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
        return_value=retrieved,
    ) as mock_retrieve:
        payment = subscription_stripe_service.record_stripe_payment_from_invoice(
            _dahlia_invoice(payments=None), succeeded=True
        )
    assert payment is not None
    assert payment.stripe_payment_intent_id == "pi_fetched"
    mock_retrieve.assert_called_once()
    assert mock_retrieve.call_args.kwargs.get("expand") == ["payments"]
    # Direct-charge Connect call must target the org's account.
    assert mock_retrieve.call_args.kwargs.get("stripe_account") == "acct_test_org"


def test_invoice_payment_intent_fetch_failure_is_tolerated(
    pending_subscription: MembershipSubscription,
) -> None:
    """A failed payments lookup must not fail the webhook — the id is best-effort."""
    with mock.patch(
        "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
        side_effect=stripe.error.StripeError("boom"),
    ):
        payment = subscription_stripe_service.record_stripe_payment_from_invoice(
            _dahlia_invoice(payments=None), succeeded=True
        )
    assert payment is not None
    assert payment.stripe_payment_intent_id == ""
    pending_subscription.refresh_from_db()
    assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE


def test_extract_client_secret_prefers_confirmation_secret() -> None:
    stripe_sub = mock.Mock(spec=stripe.Subscription)
    stripe_sub.latest_invoice = {
        "id": "in_x",
        "confirmation_secret": {"client_secret": "pi_x_secret_modern", "type": "payment_intent"},
    }
    assert subscription_stripe_service._extract_client_secret(stripe_sub) == "pi_x_secret_modern"


def test_extract_client_secret_falls_back_to_legacy_payment_intent() -> None:
    stripe_sub = mock.Mock(spec=stripe.Subscription)
    stripe_sub.latest_invoice = {"id": "in_x", "payment_intent": {"client_secret": "pi_x_secret_legacy"}}
    assert subscription_stripe_service._extract_client_secret(stripe_sub) == "pi_x_secret_legacy"
