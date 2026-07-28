"""Regression tests for the Stripe money-path fixes of the #774 review (task 1).

Every class below pins a path where Stripe was left holding — or still taking —
money the local state machine believed was settled:

* a best-effort cancel that read a schedule-managed refusal as "already gone";
* an OFFLINE staff subscription deleted by the ONLINE checkout-resume path;
* a late ``payment_failed`` rewriting a REFUNDED ledger row to FAILED;
* an invoice paid against a PAUSED row, silently.
"""

import time
import typing as t
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
from events.service import subscription_service, subscription_stripe_service, subscription_stripe_sync

pytestmark = pytest.mark.django_db

# Stripe's refusal when a SubscriptionSchedule (pending downgrade) is attached.
SCHEDULE_REFUSAL = "This subscription is managed by a subscription schedule and cannot be updated directly."
CANCEL = "events.service.subscription_stripe_service.stripe.Subscription.cancel"
# ``stripe`` is one module object, so patching it through either service module
# patches the retry inside subscription_stripe_plan_change too.
RELEASE = "events.service.subscription_stripe_plan_change.stripe.SubscriptionSchedule.release"


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    """A Stripe-connected organization."""
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
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
def offline_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier,
        name="Monthly Offline",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="money_paths_member", email="money-paths@example.com", password="pass"
    )


@pytest.fixture
def online_subscription(
    online_plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=online_plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_best_effort",
        current_period_end=timezone.now() + timedelta(days=30),
    )


def _invoice(stripe_sub_id: str, *, invoice_id: str, amount_paid: int = 1000) -> dict[str, t.Any]:
    now = int(time.time())
    return {
        "id": invoice_id,
        "subscription": stripe_sub_id,
        "amount_paid": amount_paid,
        "currency": "eur",
        "payment_intent": "pi_money_paths",
        "billing_reason": "subscription_cycle",
        "lines": {"data": [{"period": {"start": now - 86400, "end": now + 30 * 86400}}]},
    }


# ---- 1a: cancel_stripe_subscription_best_effort -------------------------------


class TestCancelBestEffortRefusals:
    """A refused ``Subscription.cancel`` must only report success when Stripe is closed.

    ``False`` is load-bearing: ``create_revival_checkout`` aborts on it rather
    than dropping the ``stripe_subscription_id`` that is the only handle back to
    a subscription Stripe would keep billing.
    """

    @pytest.mark.parametrize(
        "refusal",
        [
            stripe.error.InvalidRequestError("This subscription has been canceled.", param=None),
            stripe.error.InvalidRequestError("No such subscription: 'sub_x'", param=None, code="resource_missing"),
        ],
    )
    @mock.patch(CANCEL)
    def test_already_gone_is_success(
        self,
        mock_cancel: mock.Mock,
        online_subscription: MembershipSubscription,
        refusal: stripe.error.InvalidRequestError,
    ) -> None:
        mock_cancel.side_effect = refusal

        assert (
            subscription_stripe_service.cancel_stripe_subscription_best_effort(online_subscription, reason="ban")
            is True
        )
        assert mock_cancel.call_count == 1

    @mock.patch(RELEASE)
    @mock.patch(CANCEL)
    def test_schedule_managed_refusal_releases_and_retries(
        self,
        mock_cancel: mock.Mock,
        mock_release: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        """The canonical false positive: a pending downgrade blocks the cancel."""
        online_subscription.stripe_schedule_id = "sub_sched_1"
        online_subscription.save(update_fields=["stripe_schedule_id"])
        mock_cancel.side_effect = [
            stripe.error.InvalidRequestError(SCHEDULE_REFUSAL, param=None),
            mock.MagicMock(id="sub_best_effort"),
        ]

        assert (
            subscription_stripe_service.cancel_stripe_subscription_best_effort(
                online_subscription, reason="local_grace_expiry"
            )
            is True
        )
        mock_release.assert_called_once_with("sub_sched_1", stripe_account="acct_test_org")
        assert mock_cancel.call_count == 2
        online_subscription.refresh_from_db()
        assert online_subscription.stripe_schedule_id == ""

    @mock.patch(RELEASE)
    @mock.patch(CANCEL)
    def test_retry_after_release_still_refused_fails_closed(
        self,
        mock_cancel: mock.Mock,
        mock_release: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        online_subscription.stripe_schedule_id = "sub_sched_2"
        online_subscription.save(update_fields=["stripe_schedule_id"])
        mock_cancel.side_effect = stripe.error.InvalidRequestError(SCHEDULE_REFUSAL, param=None)

        assert (
            subscription_stripe_service.cancel_stripe_subscription_best_effort(online_subscription, reason="ban")
            is False
        )
        assert mock_cancel.call_count == 2
        mock_release.assert_called_once()

    @mock.patch(RELEASE)
    @mock.patch(CANCEL)
    def test_failed_schedule_release_never_raises(
        self,
        mock_cancel: mock.Mock,
        mock_release: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        """``release_online_schedule`` 502s on a hard Stripe error — best-effort swallows it."""
        online_subscription.stripe_schedule_id = "sub_sched_3"
        online_subscription.save(update_fields=["stripe_schedule_id"])
        mock_cancel.side_effect = stripe.error.InvalidRequestError(SCHEDULE_REFUSAL, param=None)
        mock_release.side_effect = stripe.error.APIConnectionError("connection reset")

        assert (
            subscription_stripe_service.cancel_stripe_subscription_best_effort(online_subscription, reason="ban")
            is False
        )
        assert mock_cancel.call_count == 1

    @mock.patch(RELEASE)
    @mock.patch(CANCEL)
    def test_unrelated_invalid_request_fails_closed(
        self,
        mock_cancel: mock.Mock,
        mock_release: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        """Not gone, no schedule in play: nothing to recover, so report failure."""
        mock_cancel.side_effect = stripe.error.InvalidRequestError("Invalid request: something else", param=None)

        assert (
            subscription_stripe_service.cancel_stripe_subscription_best_effort(online_subscription, reason="ban")
            is False
        )
        assert mock_cancel.call_count == 1
        mock_release.assert_not_called()

    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
    @mock.patch(RELEASE)
    @mock.patch(CANCEL)
    def test_revival_aborts_when_the_old_subscription_cannot_be_closed(
        self,
        mock_cancel: mock.Mock,
        mock_release: mock.Mock,
        mock_session: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        """The double-billing guard: no new checkout while the old sub may still bill."""
        online_subscription.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        online_subscription.expired_at = timezone.now() - timedelta(days=1)
        online_subscription.stripe_schedule_id = "sub_sched_4"
        online_subscription.save(update_fields=["status", "expired_at", "stripe_schedule_id"])
        mock_cancel.side_effect = stripe.error.InvalidRequestError(SCHEDULE_REFUSAL, param=None)

        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.create_revival_checkout(online_subscription)

        assert exc.value.status_code == 502
        mock_session.assert_not_called()
        online_subscription.refresh_from_db()
        # The only handle back to the live Stripe subscription is preserved.
        assert online_subscription.stripe_subscription_id == "sub_best_effort"
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.EXPIRED


# ---- 1b: _maybe_resume_pending_checkout is ONLINE-only ------------------------


class TestOfflinePendingRowSurvivesOnlineSubscribe:
    """A staff-created OFFLINE subscription is not an abandoned checkout.

    It carries no Checkout Session and its member is already ACTIVE, so the
    resume path used to delete a live subscription (and its ledger) the moment
    the member tried to subscribe online.
    """

    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_offline_pending_row_is_untouched_and_the_subscribe_is_refused(
        self,
        mock_customer: mock.Mock,
        mock_session: mock.Mock,
        offline_plan: MembershipSubscriptionPlan,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_offline_guard")
        offline = subscription_service.create_subscription(offline_plan, subscriber)
        assert offline.status == MembershipSubscription.SubscriptionStatus.PENDING

        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        # Refused by create_subscription's duplicate-active check, not by a delete.
        assert exc.value.status_code == 400
        mock_session.assert_not_called()
        offline.refresh_from_db()
        assert offline.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert offline.plan_id == offline_plan.pk
        assert OrganizationMember.objects.filter(organization=stripe_org, user=subscriber).exists()
        assert MembershipSubscription.objects.filter(user=subscriber, organization=stripe_org).count() == 1

    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.retrieve")
    def test_online_pending_row_is_still_resumed(
        self,
        mock_retrieve: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
    ) -> None:
        """The ONLINE narrowing must not cost the abandoned-checkout recovery."""
        pending = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_checkout_session_id="cs_open",
        )
        mock_retrieve.return_value = mock.MagicMock(
            id="cs_open", status="open", url="https://checkout.stripe.com/c/pay/cs_open"
        )

        subscription, url = subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        assert subscription.pk == pending.pk
        assert url == "https://checkout.stripe.com/c/pay/cs_open"


# ---- 1c: the stale-failure guard covers REFUNDED rows -------------------------


class TestStaleFailureNeverRewritesASettledRow:
    """A late ``payment_failed`` must not resurrect a refunded invoice as FAILED."""

    def test_refunded_row_is_left_alone(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        subscription = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_refunded",
        )
        refunded = MembershipPayment.objects.create(
            subscription=subscription,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.REFUNDED,
            stripe_invoice_id="in_refunded",
            stripe_payment_intent_id="pi_refunded",
            period_start=timezone.now() - timedelta(days=10),
            period_end=timezone.now() + timedelta(days=20),
            refund_amount=Decimal("10.00"),
            refunded_at=timezone.now(),
            stripe_refund_id="re_test",
        )

        with caplog.at_level("INFO", logger="events.service.subscription_stripe_sync"):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_refunded", invoice_id="in_refunded", amount_paid=0), succeeded=False
            )

        assert payment is not None
        assert payment.pk == refunded.pk
        refunded.refresh_from_db()
        assert refunded.status == MembershipPayment.PaymentStatus.REFUNDED
        assert refunded.amount == Decimal("10.00")
        assert refunded.refund_amount == Decimal("10.00")
        assert any("subscription_stripe_stale_payment_failed_ignored" in record.message for record in caplog.records)
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_a_first_failure_still_records_normally(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """The widened guard must not swallow genuine dunning failures."""
        subscription = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_first_failure",
        )

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice("sub_first_failure", invoice_id="in_first_failure", amount_paid=0), succeeded=False
        )

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.FAILED
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE


# ---- 1d: money arriving on a paused subscription ------------------------------


class TestPaidWhilePausedRaisesAnIncident:
    """A pre-pause invoice settling on a PAUSED row is invisible without this.

    The payment is recorded and the period advances, but the row deliberately
    stays PAUSED (staff intent wins over Stripe's Smart Retry) and every
    member-facing dispatch gate is closed — so ops needs the incident to decide
    between resuming the membership and refunding the invoice.
    """

    def test_payment_is_recorded_status_holds_and_ops_is_alerted(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        subscription = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
            stripe_subscription_id="sub_paused",
        )

        with mock.patch("events.service.stripe_incidents.record_subscription_paid_while_paused") as incident:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_paused", invoice_id="in_paused"), succeeded=True
            )

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED
        incident.assert_called_once()
        kwargs = incident.call_args.kwargs
        assert kwargs["subscription_id"] == str(subscription.pk)
        assert kwargs["user_id"] == str(subscriber.pk)
        assert kwargs["stripe_invoice_id"] == "in_paused"
        assert kwargs["amount"] == str(payment.amount)
        # Incident logs carry ids only — never the member's email.
        assert "user_email" not in kwargs

    def test_active_row_raises_no_paused_incident(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        subscription = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_active",
        )

        with mock.patch("events.service.stripe_incidents.record_subscription_paid_while_paused") as incident:
            subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_active", invoice_id="in_active"), succeeded=True
            )

        incident.assert_not_called()
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
