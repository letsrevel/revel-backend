"""Tests for Stripe-related subscription beat tasks (online expiry, reconcile)."""

import datetime
import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import plans as subscription_plans
from events.tasks import expire_subscriptions_past_grace

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="task_user", email="task@example.com", password="pass")


class TestOnlineExpiryCancelsStripe:
    """C1 (2026-06-10 reassessment): local expiry must close the Stripe side too."""

    def _make_online_sub(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        *,
        status: MembershipSubscription.SubscriptionStatus,
        period_end: datetime.datetime,
        cancel_at_period_end: bool = False,
    ) -> MembershipSubscription:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online C1",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_c1",
            stripe_product_id="prod_c1",
        )
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=status,
            cancel_at_period_end=cancel_at_period_end,
            current_period_start=period_end - datetime.timedelta(days=30),
            current_period_end=period_end,
            stripe_subscription_id="sub_c1_online",
        )

    def test_online_past_due_beyond_grace_cancels_stripe_and_notifies(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        from notifications.enums import NotificationType
        from notifications.models import Notification

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
            period_end=timezone.now() - datetime.timedelta(days=40),
        )
        with patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock:
            counters = expire_subscriptions_past_grace()

        assert counters["expired_after_grace"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_c1_online"
        assert Notification.objects.filter(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_EXPIRED
        ).exists()

    def test_online_cancel_at_period_end_terminalizes_cancelled_and_cancels_stripe(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """ONLINE cancel_at_period_end lapse → CANCELLED (not EXPIRED), Stripe
        best-effort cancel still queued so Smart Retries stop dunning."""
        from unittest.mock import patch

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            period_end=timezone.now() - datetime.timedelta(days=1),
            cancel_at_period_end=True,
        )
        with patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock:
            counters = expire_subscriptions_past_grace()

        assert counters["cancelled_at_period_end"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert sub.expired_at is None
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_c1_online"

    def test_stripe_cancel_failure_does_not_fail_the_task(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        import stripe as stripe_sdk

        sub = self._make_online_sub(
            tier,
            organization,
            subscriber,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
            period_end=timezone.now() - datetime.timedelta(days=40),
        )
        with patch(
            "events.service.subscription.stripe.checkout.stripe.Subscription.cancel",
            side_effect=stripe_sdk.error.StripeError("boom"),
        ):
            counters = expire_subscriptions_past_grace()

        assert counters["expired_after_grace"] == 1
        sub.refresh_from_db()
        # Local expiry is authoritative even when the Stripe call fails;
        # the nightly reconciliation observes the divergence later.
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED


class TestReconcileStripeSubscriptions:
    """C4 (2026-06-10 reassessment): nightly Stripe→local drift repair."""

    def _make_online_sub(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        *,
        status: MembershipSubscription.SubscriptionStatus,
        stripe_id: str = "sub_reconcile",
    ) -> MembershipSubscription:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online C4",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_c4",
            stripe_product_id="prod_c4",
        )
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=status,
            stripe_subscription_id=stripe_id,
        )

    def test_missed_paid_webhook_is_repaired(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A PENDING row whose Stripe sub went active (missed webhook) is revived."""
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING
        )
        stripe_payload = {
            "id": "sub_reconcile",
            "status": "active",
            "cancel_at_period_end": False,
            "items": {
                "data": [
                    {
                        "current_period_start": 1_800_000_000,
                        "current_period_end": 1_800_000_000 + 30 * 86400,
                        "price": {"id": "price_c4"},
                    }
                ]
            },
        }
        with patch(
            "stripe.Subscription.retrieve",
            return_value=stripe_payload,
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters == {
            "checked": 1,
            "missing": 0,
            "errors": 0,
            "stale_pending_cleared": 0,
            "ledger_backfilled": 0,
        }
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_end is not None

    def test_missing_stripe_subscription_is_counted_not_fatal(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        from unittest.mock import patch

        import stripe as stripe_sdk

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING)
        with patch(
            "stripe.Subscription.retrieve",
            side_effect=stripe_sdk.error.InvalidRequestError("No such subscription", param=None),
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters == {
            "checked": 0,
            "missing": 1,
            "errors": 0,
            "stale_pending_cleared": 0,
            "ledger_backfilled": 0,
        }

    def _terminal_row_payload(self, stripe_status: str) -> dict[str, t.Any]:
        return {
            "id": "sub_reconcile",
            "status": stripe_status,
            "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": "price_c4"}}]},
        }

    def test_terminal_row_still_live_on_stripe_is_cancelled_again(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A terminalization cancel that failed transiently is retried here.

        Without the retry the Stripe subscription keeps dunning a member who has
        already lost access locally, and a Smart Retry that succeeds bills a
        frozen row (paid_while_terminal — refundable only by hand).
        """
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.EXPIRED)
        with (
            patch("stripe.Subscription.retrieve", return_value=self._terminal_row_payload("past_due")),
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock,
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters["checked"] == 1
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_reconcile"

    def test_terminal_row_already_closed_on_stripe_is_not_cancelled_again(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """No pointless Stripe write when the two sides already agree."""
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.EXPIRED)
        with (
            patch("stripe.Subscription.retrieve", return_value=self._terminal_row_payload("canceled")),
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock,
        ):
            reconcile_stripe_subscriptions()

        cancel_mock.assert_not_called()

    def test_live_non_terminal_row_is_never_cancelled_by_reconcile(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """The repair is scoped to terminal rows — an ACTIVE member is untouched."""
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.ACTIVE)
        with (
            patch("stripe.Subscription.retrieve", return_value=self._terminal_row_payload("active")),
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock,
        ):
            reconcile_stripe_subscriptions()

        cancel_mock.assert_not_called()

    def test_old_terminal_rows_are_skipped(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Terminal rows untouched for >30 days are out of reconciliation scope."""
        from unittest.mock import patch

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.CANCELLED
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(updated_at=timezone.now() - datetime.timedelta(days=60))
        with patch("stripe.Subscription.retrieve") as retrieve_mock:
            from events.tasks.subscriptions import reconcile_stripe_subscriptions

            counters = reconcile_stripe_subscriptions()

        retrieve_mock.assert_not_called()
        assert counters == {
            "checked": 0,
            "missing": 0,
            "errors": 0,
            "stale_pending_cleared": 0,
            "ledger_backfilled": 0,
        }

    def test_stale_pending_checkout_is_cleared(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """An abandoned ONLINE checkout row (day-old, no Stripe sub) frees its cap slot."""
        from unittest.mock import MagicMock, patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING, stripe_id=""
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(
            stripe_checkout_session_id="cs_stale",
            updated_at=timezone.now() - datetime.timedelta(days=2),
        )
        with (
            patch("stripe.Subscription.retrieve") as retrieve_mock,
            patch(
                "events.service.subscription.stripe.checkout.stripe.checkout.Session.retrieve",
                return_value=MagicMock(id="cs_stale", status="expired"),
            ),
        ):
            counters = reconcile_stripe_subscriptions()

        retrieve_mock.assert_not_called()
        assert counters["stale_pending_cleared"] == 1
        assert not MembershipSubscription.objects.filter(pk=sub.pk).exists()

    def test_stale_pending_revival_row_reverts_to_expired(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A stale revival checkout keeps its ledger: reverted to EXPIRED, not deleted."""
        from unittest.mock import MagicMock, patch

        from events.models import MembershipPayment
        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING, stripe_id=""
        )
        now = timezone.now()
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            period_start=now - datetime.timedelta(days=60),
            period_end=now - datetime.timedelta(days=30),
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(
            stripe_checkout_session_id="cs_stale_revival",
            expired_at=now - datetime.timedelta(days=10),
            updated_at=now - datetime.timedelta(days=2),
        )
        with (
            patch("stripe.Subscription.retrieve"),
            patch(
                "events.service.subscription.stripe.checkout.stripe.checkout.Session.retrieve",
                return_value=MagicMock(id="cs_stale_revival", status="expired"),
            ),
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters["stale_pending_cleared"] == 1
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.stripe_checkout_session_id == ""
        assert sub.expired_at is not None  # revival window preserved

    def test_recent_pending_checkout_is_left_alone(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A live checkout (row touched within a day) is not swept."""
        from unittest.mock import patch

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.PENDING, stripe_id=""
        )
        MembershipSubscription.objects.filter(pk=sub.pk).update(stripe_checkout_session_id="cs_live")
        with patch("stripe.Subscription.retrieve"):
            counters = reconcile_stripe_subscriptions()

        assert counters["stale_pending_cleared"] == 0
        assert MembershipSubscription.objects.filter(pk=sub.pk).exists()

    def test_ledger_backfill_records_missed_paid_invoice(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A paid latest_invoice unknown locally is recorded (dropped invoice.paid)."""
        from unittest.mock import patch

        from events.models import MembershipPayment
        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.ACTIVE
        )
        period_start = 1_800_000_000
        period_end = period_start + 30 * 86400
        stripe_payload = {
            "id": "sub_reconcile",
            "status": "active",
            "cancel_at_period_end": False,
            "items": {
                "data": [
                    {
                        "current_period_start": period_start,
                        "current_period_end": period_end,
                        "price": {"id": "price_c4"},
                    }
                ]
            },
            "latest_invoice": {
                "id": "in_backfilled",
                "status": "paid",
                "subscription": "sub_reconcile",
                "payment_intent": "pi_backfilled",
                "currency": "eur",
                "amount_paid": 1000,
                "lines": {"data": [{"period": {"start": period_start, "end": period_end}}]},
            },
        }
        with patch("stripe.Subscription.retrieve", return_value=stripe_payload):
            counters = reconcile_stripe_subscriptions()

        assert counters["ledger_backfilled"] == 1
        payment = MembershipPayment.objects.get(stripe_invoice_id="in_backfilled")
        assert payment.subscription_id == sub.pk
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.amount == Decimal("10.00")

    def test_ledger_backfill_failure_does_not_abort_the_sweep(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """An unresolvable backfill (e.g. its stub Price can't be read) is counted and skipped, not raised."""
        from unittest.mock import patch

        import stripe

        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        self._make_online_sub(tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.ACTIVE)
        stripe_payload = {
            "id": "sub_reconcile",
            "status": "active",
            "cancel_at_period_end": False,
            "items": {"data": [{"current_period_start": 1_800_000_000, "current_period_end": 1_802_592_000}]},
            "latest_invoice": {"id": "in_unresolvable", "status": "paid", "subscription": "sub_reconcile"},
        }
        with (
            patch("stripe.Subscription.retrieve", return_value=stripe_payload),
            patch(
                "events.service.subscription.stripe.sync.record_stripe_payment_from_invoice",
                side_effect=stripe.error.APIConnectionError("stripe down"),
            ),
        ):
            counters = reconcile_stripe_subscriptions()

        assert counters["errors"] == 1
        assert counters["ledger_backfilled"] == 0

    def test_ledger_backfill_never_touches_known_invoices(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A known invoice id (e.g. an already-REFUNDED row) is never resurrected."""
        from unittest.mock import patch

        from events.models import MembershipPayment
        from events.tasks.subscriptions import reconcile_stripe_subscriptions

        sub = self._make_online_sub(
            tier, organization, subscriber, status=MembershipSubscription.SubscriptionStatus.ACTIVE
        )
        now = timezone.now()
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.REFUNDED,
            stripe_invoice_id="in_known",
            period_start=now - datetime.timedelta(days=30),
            period_end=now,
        )
        stripe_payload = {
            "id": "sub_reconcile",
            "status": "active",
            "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": "price_c4"}}]},
            "latest_invoice": {
                "id": "in_known",
                "status": "paid",
                "subscription": "sub_reconcile",
                "currency": "eur",
                "amount_paid": 1000,
            },
        }
        with patch("stripe.Subscription.retrieve", return_value=stripe_payload):
            counters = reconcile_stripe_subscriptions()

        assert counters["ledger_backfilled"] == 0
        payment = MembershipPayment.objects.get(stripe_invoice_id="in_known")
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
