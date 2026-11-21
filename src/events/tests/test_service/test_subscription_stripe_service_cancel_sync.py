"""Tests for cancelling, syncing and recording Stripe-backed membership subscriptions."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync

pytestmark = pytest.mark.django_db


def _make_stripe_connected(org: Organization) -> None:
    """Flip the Stripe Connect flags on an organization."""
    org.stripe_account_id = "acct_test_org"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    """A Stripe-connected organization."""
    _make_stripe_connected(organization)
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """An ONLINE plan with pre-populated Stripe IDs (skips ensure_stripe_price)."""
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
    return django_user_model.objects.create_user(
        username="online_subscriber", email="online@example.com", password="pass"
    )


# ---- cancel_online_subscription ---------------------------------------------


class TestCancelOnlineSubscription:
    @pytest.fixture
    def online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_abc",
            current_period_end=timezone.now() + timedelta(days=30),
        )
        return sub

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify")
    def test_schedules_at_period_end(self, mock_modify: mock.Mock, online_subscription: MembershipSubscription) -> None:
        result = subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=False)

        mock_modify.assert_called_once_with("sub_abc", cancel_at_period_end=True, stripe_account="acct_test_org")
        assert result.cancel_at_period_end is True
        # Status stays ACTIVE until the period end webhook arrives.
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel")
    def test_immediate_cancellation_marks_terminal(
        self, mock_cancel: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        result = subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=True)

        mock_cancel.assert_called_once_with("sub_abc", stripe_account="acct_test_org")
        assert result.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert result.cancelled_at is not None

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel")
    def test_immediate_cancellation_tolerates_already_canceled_on_stripe(
        self, mock_cancel: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """A Stripe-side already-canceled sub must not 500 the cancel path.

        Scenario: staff cancels + refunds in the Stripe Dashboard; the
        ``charge.refunded`` webhook's auto-cancel then calls Stripe against an
        already-canceled subscription. Raising would roll back the webhook
        dedup row and wedge Stripe redelivery in a permanent retry loop.
        """
        mock_cancel.side_effect = stripe.error.InvalidRequestError("This subscription has been canceled.", param=None)

        result = subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=True)

        assert result.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert result.cancelled_at is not None

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify")
    def test_period_end_cancellation_tolerates_already_canceled_on_stripe(
        self, mock_modify: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """Same race on the at-period-end branch: record intent, don't 500."""
        mock_modify.side_effect = stripe.error.InvalidRequestError("This subscription has been canceled.", param=None)

        result = subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=False)

        assert result.cancel_at_period_end is True

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify")
    def test_period_end_cancellation_tolerates_resource_missing(
        self, mock_modify: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """A subscription Stripe no longer knows about is already in the desired end state."""
        mock_modify.side_effect = stripe.error.InvalidRequestError(
            "No such subscription: 'sub_abc'", param=None, code="resource_missing"
        )

        result = subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=False)

        assert result.cancel_at_period_end is True

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify")
    def test_period_end_cancellation_does_not_swallow_other_invalid_requests(
        self, mock_modify: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """A rejection that is NOT "already gone" leaves Stripe billing — never record the intent.

        The canonical case is a schedule-managed subscription (a downgrade
        schedule still running), which Stripe refuses to modify. Swallowing it
        marked the row cancelled locally while Stripe renewed it indefinitely.
        """
        mock_modify.side_effect = stripe.error.InvalidRequestError(
            "This subscription is managed by a subscription schedule and cannot be updated directly.",
            param="cancel_at_period_end",
        )

        with pytest.raises(HttpError) as exc:
            subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=False)

        assert exc.value.status_code == 502
        online_subscription.refresh_from_db()
        assert online_subscription.cancel_at_period_end is False
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel")
    def test_immediate_cancellation_does_not_swallow_other_invalid_requests(
        self, mock_cancel: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """Same guard on the immediate branch: no local terminalization Stripe refused."""
        mock_cancel.side_effect = stripe.error.InvalidRequestError(
            "This subscription is managed by a subscription schedule and cannot be updated directly.",
            param=None,
        )

        with pytest.raises(HttpError) as exc:
            subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=True)

        assert exc.value.status_code == 502
        online_subscription.refresh_from_db()
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert online_subscription.cancelled_at is None

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel")
    def test_immediate_cancellation_wraps_transient_stripe_error(
        self, mock_cancel: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """Transient Stripe failures surface as the module's retryable 502, leaving state untouched."""
        mock_cancel.side_effect = stripe.error.APIConnectionError("connection reset")

        with pytest.raises(HttpError) as exc:
            subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=True)

        assert exc.value.status_code == 502
        online_subscription.refresh_from_db()
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify")
    def test_period_end_cancellation_wraps_transient_stripe_error(
        self, mock_modify: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        """The scheduled branch must not record a cancel intent Stripe never accepted."""
        mock_modify.side_effect = stripe.error.APIConnectionError("connection reset")

        with pytest.raises(HttpError) as exc:
            subscription_stripe_checkout.cancel_online_subscription(online_subscription, immediate=False)

        assert exc.value.status_code == 502
        online_subscription.refresh_from_db()
        assert online_subscription.cancel_at_period_end is False


# ---- sync_subscription_from_stripe ------------------------------------------


class TestSyncSubscriptionFromStripe:
    @pytest.fixture
    def pending_online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_to_sync",
        )

    def test_mirrors_active_status_and_periods(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        payload = {
            "id": "sub_to_sync",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": 1_800_000_000,
            "current_period_end": 1_800_000_000 + 30 * 86400,
        }
        result = subscription_stripe_sync.sync_subscription_from_stripe(payload)
        assert result is not None
        result.refresh_from_db()
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert result.current_period_start is not None
        assert result.current_period_end is not None
        # _ensure_active_member should have created the member at the plan tier.
        member = OrganizationMember.objects.get(
            organization=pending_online_subscription.organization, user=pending_online_subscription.user
        )
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == pending_online_subscription.plan.tier_id

    def test_unknown_subscription_returns_none(self) -> None:
        result = subscription_stripe_sync.sync_subscription_from_stripe({"id": "sub_unknown", "status": "active"})
        assert result is None

    def test_cancel_at_period_end_mirrored(self, pending_online_subscription: MembershipSubscription) -> None:
        payload = {
            "id": "sub_to_sync",
            "status": "active",
            "cancel_at_period_end": True,
        }
        result = subscription_stripe_sync.sync_subscription_from_stripe(payload)
        assert result is not None
        assert result.cancel_at_period_end is True


# ---- record_stripe_payment_from_invoice -------------------------------------


class TestRecordStripePaymentFromInvoice:
    @pytest.fixture
    def pending_online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_invoice",
        )

    def test_succeeded_creates_payment_and_activates_subscription(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        invoice = {
            "id": "in_test",
            "subscription": "sub_invoice",
            "amount_paid": 1000,
            "currency": "eur",
            "payment_intent": "pi_test",
            "lines": {
                "data": [
                    {
                        "period": {
                            "start": 1_800_000_000,
                            "end": 1_800_000_000 + 30 * 86400,
                        }
                    }
                ]
            },
        }
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.amount == Decimal("10.00")
        assert payment.stripe_invoice_id == "in_test"
        assert payment.stripe_payment_intent_id == "pi_test"

        pending_online_subscription.refresh_from_db()
        assert pending_online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        # Member created via _ensure_active_member.
        member = OrganizationMember.objects.get(
            organization=pending_online_subscription.organization,
            user=pending_online_subscription.user,
        )
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_failed_payment_records_and_transitions_to_past_due(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        pending_online_subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        pending_online_subscription.save(update_fields=["status"])

        invoice = {
            "id": "in_fail",
            "subscription": "sub_invoice",
            "amount_paid": 0,
            "amount_due": 1000,
            "currency": "eur",
            "payment_intent": "pi_fail",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 86400}}]},
        }

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)
        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.FAILED
        # FAILED payments collected nothing — ``amount`` reflects that.
        # ``raw_response`` preserves Stripe's reported ``amount_due``.
        assert payment.amount == Decimal("0")
        assert payment.raw_response.get("amount_due") == 1000

        pending_online_subscription.refresh_from_db()
        assert pending_online_subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE

    def test_duplicate_webhook_is_idempotent(self, pending_online_subscription: MembershipSubscription) -> None:
        invoice = {
            "id": "in_dup",
            "subscription": "sub_invoice",
            "amount_paid": 1000,
            "currency": "eur",
            "payment_intent": "pi_dup",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 86400}}]},
        }
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert MembershipPayment.objects.filter(stripe_invoice_id="in_dup").count() == 1

    def test_unknown_subscription_returns_none(self) -> None:
        invoice = {
            "id": "in_orphan",
            "subscription": "sub_nope",
            "amount_paid": 0,
            "currency": "eur",
        }
        assert subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False) is None

    def test_stale_payment_failed_never_downgrades_succeeded_payment(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        """Out-of-order delivery: ``payment_failed`` arriving after ``paid``.

        Stripe guarantees no event ordering, and a failed→retried→paid invoice
        emits both events. The late ``failed`` must neither corrupt the
        SUCCEEDED ledger row (amount would drop to 0) nor push the ACTIVE
        subscription back to PAST_DUE.
        """
        invoice = {
            "id": "in_ooo",
            "subscription": "sub_invoice",
            "amount_paid": 1000,
            "currency": "eur",
            "payment_intent": "pi_ooo",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 86400}}]},
        }
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        stale_failure = {**invoice, "amount_paid": 0, "amount_due": 1000}
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(stale_failure, succeeded=False)

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.amount == Decimal("10.00")
        assert MembershipPayment.objects.filter(stripe_invoice_id="in_ooo").count() == 1

        pending_online_subscription.refresh_from_db()
        assert pending_online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
