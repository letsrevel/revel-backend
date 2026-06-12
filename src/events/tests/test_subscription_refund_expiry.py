"""Tests for refund-triggered subscription expiry."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service
from events.service.stripe_webhooks import StripeEventHandler


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="ref_user", email="ref_user@example.com", password="pass")


@pytest.fixture
def active_sub(
    plan: MembershipSubscriptionPlan, organization: Organization, subscriber: RevelUser
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=timezone.now() - timedelta(days=10),
        current_period_end=timezone.now() + timedelta(days=20),
    )


@pytest.mark.django_db
class TestRefundAutoCancel:
    def test_full_refund_of_current_period_cancels_subscription(
        self, active_sub: MembershipSubscription, plan: MembershipSubscriptionPlan
    ) -> None:
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
        )
        subscription_service.refund_payment(payment, recorded_by=None)
        active_sub.refresh_from_db()
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert active_sub.cancelled_at is not None

    def test_refund_of_old_period_does_not_cancel(
        self, active_sub: MembershipSubscription, plan: MembershipSubscriptionPlan
    ) -> None:
        assert active_sub.current_period_start is not None
        old_start = active_sub.current_period_start - timedelta(days=60)
        old_end = active_sub.current_period_start - timedelta(days=30)
        old_payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=old_start,
            period_end=old_end,
        )
        subscription_service.refund_payment(old_payment, recorded_by=None)
        active_sub.refresh_from_db()
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_partial_refund_does_not_cancel(
        self, active_sub: MembershipSubscription, plan: MembershipSubscriptionPlan
    ) -> None:
        """Two SUCCEEDED payments for the same period; only one refunded — not full."""
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
        )
        small_payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=Decimal("1.00"),  # smaller than the main one
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
        )
        subscription_service.refund_payment(small_payment, recorded_by=None)
        active_sub.refresh_from_db()
        # Only 1.00 of 11.00 refunded → not a full refund → sub still ACTIVE
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_refund_on_already_cancelled_sub_is_noop(
        self, active_sub: MembershipSubscription, plan: MembershipSubscriptionPlan
    ) -> None:
        """Refunding when the sub is already CANCELLED doesn't break anything."""
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        active_sub.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        active_sub.cancelled_at = timezone.now()
        active_sub.save(update_fields=["status", "cancelled_at", "updated_at"])
        payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
        )
        subscription_service.refund_payment(payment, recorded_by=None)
        active_sub.refresh_from_db()
        # cancel_subscription early-returns for terminal subs → no state change
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_refund_already_refunded_payment_is_idempotent(
        self, active_sub: MembershipSubscription, plan: MembershipSubscriptionPlan
    ) -> None:
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.REFUNDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
        )
        result = subscription_service.refund_payment(payment, recorded_by=None)
        active_sub.refresh_from_db()
        # Already REFUNDED → early return → sub state unchanged
        assert result.status == MembershipPayment.PaymentStatus.REFUNDED
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE


def _subscription_charge_event(payment_intent_id: str, amount_cents: int) -> stripe.Event:
    """Build a minimal charge.refunded MagicMock compatible with StripeEventHandler."""
    ev: stripe.Event = MagicMock(spec=stripe.Event)
    ev.type = "charge.refunded"
    ev.data = MagicMock()
    ev.data.object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "amount_refunded": amount_cents,
        "amount": amount_cents,
        "refunds": {"data": [{"id": "re_test_1", "amount": amount_cents}]},
    }
    # Make dict(event) serializable for raw_response — empty is fine for these tests.
    ev.__iter__.return_value = iter([])  # type: ignore[attr-defined]
    return ev


@pytest.mark.django_db
class TestChargeRefundedWebhook:
    """Tests for charge.refunded routing to MembershipPayment."""

    def test_full_refund_via_webhook_cancels_subscription(
        self,
        active_sub: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
            stripe_payment_intent_id="pi_test_123",
        )

        event = _subscription_charge_event("pi_test_123", int(plan.price * 100))
        StripeEventHandler(event).handle_charge_refunded(event)

        payment.refresh_from_db()
        active_sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_redelivered_refund_webhook_is_idempotent(
        self,
        active_sub: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        assert active_sub.current_period_start is not None
        assert active_sub.current_period_end is not None
        # Already REFUNDED — simulates a re-delivered webhook.
        payment = MembershipPayment.objects.create(
            subscription=active_sub,
            amount=plan.price,
            currency=plan.currency,
            status=MembershipPayment.PaymentStatus.REFUNDED,
            period_start=active_sub.current_period_start,
            period_end=active_sub.current_period_end,
            stripe_payment_intent_id="pi_test_redelivery",
        )

        event = _subscription_charge_event("pi_test_redelivery", int(plan.price * 100))
        StripeEventHandler(event).handle_charge_refunded(event)

        payment.refresh_from_db()
        active_sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        # Sub state unchanged (was ACTIVE; idempotent re-call does not cancel).
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_ticket_refund_falls_through_subscription_branch(
        self,
        active_sub: MembershipSubscription,
    ) -> None:
        """A charge.refunded for a payment_intent_id that doesn't match any
        MembershipPayment falls through to the existing ticket-refund logic.
        Verify the subscription branch doesn't crash on a non-match.
        """
        event = _subscription_charge_event("pi_nonexistent_ticket_payment", 1000)
        # The ticket-refund path logs a warning for unknown intent — that's fine.
        StripeEventHandler(event).handle_charge_refunded(event)

        active_sub.refresh_from_db()
        # Subscription state is untouched.
        assert active_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
