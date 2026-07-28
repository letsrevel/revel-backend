"""Payload-shape tests for API versions >= 2025-03-31.basil (pinned: dahlia).

The pinned webhook endpoints render Subscription/Invoice payloads with:
- ``current_period_{start,end}`` on subscription items, not the Subscription;
- the subscription reference at ``invoice.parent.subscription_details``;
- ``invoice.payment_intent`` replaced by the ``payments`` list (not embedded
  in webhook payloads — requires an outbound expand).

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
from events.service import subscription_stripe_payloads, subscription_stripe_sync

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
    result = subscription_stripe_sync.sync_subscription_from_stripe(payload)
    assert result is not None
    result.refresh_from_db()
    assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert result.current_period_start is not None
    assert result.current_period_end is not None


def _dahlia_invoice(
    payments: dict[str, t.Any] | None,
    *,
    invoice_id: str = "in_dahlia",
    lines: list[dict[str, t.Any]] | None = None,
) -> dict[str, t.Any]:
    invoice: dict[str, t.Any] = {
        "id": invoice_id,
        "amount_paid": 1000,
        "currency": "eur",
        # basil+ shape: no top-level "subscription" / "payment_intent".
        "parent": {"subscription_details": {"subscription": "sub_dahlia", "metadata": {}}},
        "lines": {"data": lines or [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 30 * 86400}}]},
    }
    if payments is not None:
        invoice["payments"] = payments
    return invoice


def _dahlia_line(
    *,
    price_id: str,
    start: int,
    end: int,
    proration: bool,
    parent_type: str = "subscription_item_details",
) -> dict[str, t.Any]:
    """A basil+ invoice line: price under ``pricing``, ``proration`` under ``parent``."""
    return {
        "pricing": {"price_details": {"price": price_id}},
        "parent": {"type": parent_type, parent_type: {"proration": proration}},
        "period": {"start": start, "end": end},
    }


def test_invoice_paid_resolves_subscription_via_parent_details(
    pending_subscription: MembershipSubscription,
) -> None:
    payments = {
        "data": [
            {
                "payment": {
                    "type": "payment_intent",
                    # Expanded PaymentIntent — the shape a deep-expanded retrieve
                    # (e.g. the checkout backfill) passes through.
                    "payment_intent": {"id": "pi_dahlia", "application_fee_amount": None},
                }
            }
        ]
    }
    payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_dahlia_invoice(payments), succeeded=True)
    assert payment is not None
    assert payment.subscription_id == pending_subscription.pk
    assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
    assert payment.stripe_payment_intent_id == "pi_dahlia"
    pending_subscription.refresh_from_db()
    assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE


def test_invoice_without_embedded_payments_fetches_outbound(
    pending_subscription: MembershipSubscription,
) -> None:
    """Webhook payloads don't embed ``payments`` — resolve via Invoice.retrieve.

    The pinned dahlia version also removed the Invoice's readable
    ``application_fee_amount``, so the same fetch must expand down to the
    PaymentIntent and surface the collected fee (a zero fee here would silently
    drop every ONLINE membership fee from revenue and referral payouts).
    """
    retrieved = {
        "payments": {
            "data": [
                {
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": {"id": "pi_fetched", "application_fee_amount": 180},
                    }
                }
            ]
        }
    }
    with mock.patch(
        "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
        return_value=retrieved,
    ) as mock_retrieve:
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
            _dahlia_invoice(payments=None), succeeded=True
        )
    assert payment is not None
    assert payment.stripe_payment_intent_id == "pi_fetched"
    assert payment.platform_fee == Decimal("1.80")
    mock_retrieve.assert_called_once()
    assert mock_retrieve.call_args.kwargs.get("expand") == ["payments.data.payment.payment_intent"]
    # Direct-charge Connect call must target the org's account.
    assert mock_retrieve.call_args.kwargs.get("stripe_account") == "acct_test_org"


def test_embedded_unexpanded_payment_still_resolves_fee_outbound(
    pending_subscription: MembershipSubscription,
) -> None:
    """A payments list with a bare intent id gives no fee — a paid invoice fetches it."""
    payments = {"data": [{"payment": {"type": "payment_intent", "payment_intent": "pi_bare"}}]}
    retrieved = {
        "payments": {
            "data": [
                {
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": {"id": "pi_bare", "application_fee_amount": 180},
                    }
                }
            ]
        }
    }
    with mock.patch(
        "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
        return_value=retrieved,
    ) as mock_retrieve:
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_dahlia_invoice(payments), succeeded=True)
    assert payment is not None
    assert payment.stripe_payment_intent_id == "pi_bare"
    assert payment.platform_fee == Decimal("1.80")
    mock_retrieve.assert_called_once()


def test_invoice_payment_intent_fetch_failure_is_tolerated(
    pending_subscription: MembershipSubscription,
) -> None:
    """A failed payments lookup must not fail the webhook — the id is best-effort."""
    with mock.patch(
        "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
        side_effect=stripe.error.StripeError("boom"),
    ):
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
            _dahlia_invoice(payments=None), succeeded=True
        )
    assert payment is not None
    assert payment.stripe_payment_intent_id == ""
    pending_subscription.refresh_from_db()
    assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE


class TestDahliaProrationDetection:
    """``proration`` moved under ``line.parent`` in the pinned API version.

    Reading only the legacy top-level flag makes every dahlia proration look
    like a recurring line, so a mid-cycle upgrade's proration-only invoice
    would rewrite ``current_period_start`` to the proration window — the exact
    anchor corruption ``_recurring_line_period`` exists to prevent.
    """

    ANCHOR = 1_800_000_000

    def test_subscription_item_proration_only_invoice_has_no_billing_period(self) -> None:
        lines = [
            _dahlia_line(price_id="price_test", start=self.ANCHOR, end=self.ANCHOR + 86400, proration=True),
        ]
        assert subscription_stripe_sync._recurring_line_period(lines, "price_test") is None

    def test_invoice_item_proration_is_detected(self) -> None:
        lines = [
            _dahlia_line(
                price_id="price_test",
                start=self.ANCHOR,
                end=self.ANCHOR + 86400,
                proration=True,
                parent_type="invoice_item_details",
            ),
        ]
        assert subscription_stripe_sync._recurring_line_period(lines, "price_test") is None

    def test_recurring_line_wins_over_a_leading_proration(self) -> None:
        recurring_start, recurring_end = self.ANCHOR + 86400, self.ANCHOR + 31 * 86400
        lines = [
            _dahlia_line(price_id="price_test", start=self.ANCHOR, end=self.ANCHOR + 100, proration=True),
            _dahlia_line(price_id="price_test", start=recurring_start, end=recurring_end, proration=False),
        ]
        assert subscription_stripe_sync._recurring_line_period(lines, "price_test") == {
            "start": recurring_start,
            "end": recurring_end,
        }

    def test_legacy_top_level_proration_flag_still_honored(self) -> None:
        lines: list[dict[str, t.Any]] = [
            {"proration": True, "period": {"start": self.ANCHOR, "end": self.ANCHOR + 86400}}
        ]
        assert subscription_stripe_sync._recurring_line_period(lines, "price_test") is None

    def test_proration_only_invoice_leaves_the_anchor_alone(
        self,
        pending_subscription: MembershipSubscription,
    ) -> None:
        """End-to-end: the upgrade's proration invoice must not move the anchor."""
        anchor_start = subscription_stripe_payloads._epoch_to_dt(self.ANCHOR - 10 * 86400)
        anchor_end = subscription_stripe_payloads._epoch_to_dt(self.ANCHOR + 20 * 86400)
        pending_subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        pending_subscription.current_period_start = anchor_start
        pending_subscription.current_period_end = anchor_end
        pending_subscription.save(update_fields=["status", "current_period_start", "current_period_end"])

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _dahlia_invoice(
                {
                    "data": [
                        {
                            "payment": {
                                "type": "payment_intent",
                                "payment_intent": {"id": "pi_proration", "application_fee_amount": None},
                            }
                        }
                    ]
                },
                invoice_id="in_dahlia_proration",
                lines=[
                    _dahlia_line(price_id="price_test", start=self.ANCHOR, end=self.ANCHOR + 20 * 86400, proration=True)
                ],
            ),
            succeeded=True,
        )

        pending_subscription.refresh_from_db()
        assert pending_subscription.current_period_start == anchor_start
        assert pending_subscription.current_period_end == anchor_end
        # The ledger row still records what the proration actually covered.
        payment = MembershipPayment.objects.get(stripe_invoice_id="in_dahlia_proration")
        assert int(payment.period_start.timestamp()) == self.ANCHOR


@pytest.mark.parametrize(
    ("message", "code", "expected"),
    [
        ("No such subscription: 'sub_x'", "resource_missing", True),
        ("This subscription has been canceled.", None, True),
        ("A canceled subscription can only update its cancellation_details.", None, True),
        ("You cannot update a subscription that is canceled.", None, True),
        (
            "You cannot set `cancel_at_period_end` on a subscription managed by a subscription schedule.",
            None,
            False,
        ),
        ("This subscription is managed by a subscription schedule.", None, False),
        ("Invalid `proration_behavior`: must be one of always_invoice, create_prorations, none", None, False),
    ],
)
def test_is_subscription_gone_classification(message: str, code: str | None, expected: bool) -> None:
    """Only a missing/canceled subscription counts as gone — never a schedule-managed refusal.

    Swallowing the schedule-managed rejection is what let a member's cancel
    no-op on Stripe while the local row said "cancelled" (they kept being billed).
    """
    exc = stripe.error.InvalidRequestError(message, param=None, code=code)
    assert subscription_stripe_payloads._is_subscription_gone(exc) is expected
