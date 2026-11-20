"""Tests for the subscription service lifecycle, refunds and checkout expiry."""

from decimal import Decimal
from unittest import mock

import pytest
import stripe
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import (
    SubscriptionActivationPendingError,
)
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans
from events.service.subscription import refunds as subscription_refunds
from events.service.subscription.lifecycle import InitialPayment

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """Use the default tier auto-created on organization save."""
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A monthly EUR plan."""
    return subscription_plans.create_plan(
        tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who will subscribe."""
    return django_user_model.objects.create_user(username="subscriber", email="subscriber@example.com", password="pass")


@pytest.fixture
def recorder(organization_owner_user: RevelUser) -> RevelUser:
    """Staff user recording payments."""
    return organization_owner_user


# ---- Plan CRUD ---------------------------------------------------------------


# ---- cancel / pause / resume -------------------------------------------------


class TestLifecycle:
    @staticmethod
    def _paid(plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser) -> MembershipSubscription:
        """A subscription past its first payment (a never-paid offline one can't be paused)."""
        return subscription_lifecycle.create_subscription(
            plan,
            subscriber,
            initial_payment=subscription_lifecycle.InitialPayment(
                amount=plan.price, currency=plan.currency, recorded_by=recorder
            ),
        )

    def test_cancel_at_period_end(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        # The initial payment gives the row a period boundary to cancel at;
        # without one the scheduled cancel is upgraded to an immediate one.
        sub = subscription_lifecycle.create_subscription(
            plan,
            subscriber,
            initial_payment=subscription_lifecycle.InitialPayment(
                amount=plan.price, currency=plan.currency, recorded_by=recorder
            ),
        )
        out = subscription_lifecycle.cancel_subscription(sub, immediate=False)
        assert out.cancel_at_period_end is True
        assert out.status != MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_cancel_immediate(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        out = subscription_lifecycle.cancel_subscription(sub, immediate=True)
        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert out.cancelled_at is not None

    def test_cancel_terminal_is_idempotent(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        subscription_lifecycle.cancel_subscription(sub, immediate=True)
        again = subscription_lifecycle.cancel_subscription(sub, immediate=True)
        assert again.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_pause_and_resume(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = self._paid(plan, subscriber, recorder)
        paused = subscription_lifecycle.pause_subscription(sub)
        assert paused.status == MembershipSubscription.SubscriptionStatus.PAUSED

        resumed = subscription_lifecycle.resume_subscription(paused)
        assert resumed.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_pause_idempotent(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = self._paid(plan, subscriber, recorder)
        subscription_lifecycle.pause_subscription(sub)
        again = subscription_lifecycle.pause_subscription(sub)
        assert again.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_pause_terminal_blocked(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        subscription_lifecycle.cancel_subscription(sub, immediate=True)
        with pytest.raises(HttpError):
            subscription_lifecycle.pause_subscription(sub)

    def test_resume_blocked_when_not_paused(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        with pytest.raises(HttpError):
            subscription_lifecycle.resume_subscription(sub)

    def test_schedule_cancel_blocked_when_paused(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """A paused subscription cannot be scheduled to cancel at period end.

        Time is frozen while PAUSED, so the period boundary would never be
        reached. Callers must resume first or cancel immediately.
        """
        sub = self._paid(plan, subscriber, recorder)
        subscription_lifecycle.pause_subscription(sub)
        with pytest.raises(HttpError) as excinfo:
            subscription_lifecycle.cancel_subscription(sub, immediate=False)
        assert excinfo.value.status_code == 400

    def test_cancel_immediate_works_when_paused(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = self._paid(plan, subscriber, recorder)
        subscription_lifecycle.pause_subscription(sub)
        out = subscription_lifecycle.cancel_subscription(sub, immediate=True)
        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_schedule_cancel_blocked_when_paused_online(self, tier: MembershipTier, subscriber: RevelUser) -> None:
        """The PAUSED guard must gate the ONLINE branch too.

        Regression: the guard used to live only in the OFFLINE else-branch, so
        a PAUSED ONLINE subscription could take the scheduled path — leaving
        the row PAUSED with ``cancel_at_period_end=True``, invisible to the
        grace-expiry sweep (which only selects ACTIVE/PAST_DUE).
        """
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online Paused Cancel",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
            stripe_subscription_id="sub_paused_online",
        )
        with pytest.raises(HttpError) as excinfo:
            subscription_lifecycle.cancel_subscription(sub, immediate=False)
        assert excinfo.value.status_code == 400
        sub.refresh_from_db()
        assert sub.cancel_at_period_end is False
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_pause_blocked_when_cancel_scheduled(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        recorder: RevelUser,
    ) -> None:
        """The reverse of the PAUSED-cancel guard.

        A subscription with a scheduled cancel cannot be paused — pausing would
        freeze it PAUSED+cancel_at_period_end forever, invisible to the
        grace-expiry sweep (which only selects ACTIVE/PAST_DUE).
        """
        sub = subscription_lifecycle.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        subscription_lifecycle.cancel_subscription(sub, immediate=False)
        with pytest.raises(HttpError) as excinfo:
            subscription_lifecycle.pause_subscription(sub)
        assert excinfo.value.status_code == 400
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.cancel_at_period_end is True

    def test_pause_blocked_when_cancel_scheduled_online(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """The scheduled-cancel pause guard gates the ONLINE branch too, before any Stripe call."""
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online Cancel Scheduled",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_cancel_scheduled",
            cancel_at_period_end=True,
        )
        with mock.patch("events.service.subscription.stripe.checkout.pause_online_subscription") as mock_pause:
            with pytest.raises(HttpError) as excinfo:
                subscription_lifecycle.pause_subscription(sub)
        assert excinfo.value.status_code == 400
        mock_pause.assert_not_called()
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.cancel_at_period_end is True

    def test_pause_online_without_stripe_id_is_refused(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """An ONLINE row that hasn't been linked to a Stripe subscription must not pause locally."""
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        # stripe_subscription_id intentionally empty
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as exc:
            subscription_lifecycle.pause_subscription(sub)
        assert exc.value.status_code == 400

    def test_resume_online_without_stripe_id_is_refused(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online R",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
        )
        with pytest.raises(HttpError) as exc:
            subscription_lifecycle.resume_subscription(sub)
        assert exc.value.status_code == 400


# ---- refund_payment ----------------------------------------------------------


class TestRefundPayment:
    def test_refund_full_current_period_auto_cancels_subscription(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """Phase 4: a full refund of the subscription's current period
        auto-cancels the subscription immediately. The previous record-only
        contract was replaced in Phase 4 (§2 of the design)."""
        sub = subscription_lifecycle.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        payment = sub.payments.first()
        assert payment is not None

        refunded = subscription_refunds.refund_payment(payment, recorded_by=recorder, notes="customer asked")
        assert refunded.status == MembershipPayment.PaymentStatus.REFUNDED

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert sub.cancelled_at is not None

    def test_refund_idempotent(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_lifecycle.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        payment = sub.payments.first()
        assert payment is not None
        subscription_refunds.refund_payment(payment, recorded_by=recorder)
        again = subscription_refunds.refund_payment(payment, recorded_by=recorder)
        assert again.status == MembershipPayment.PaymentStatus.REFUNDED


# ---- immediate cancel vs. a still-payable Checkout Session --------------------


class TestImmediateCancelExpiresCheckout:
    """An immediate cancel must kill the Checkout Session it would otherwise strand.

    An ONLINE PENDING row carries a live hosted Checkout the member can still
    complete from an open tab. Terminalizing locally without expiring it lets
    Stripe mint a Subscription that keeps billing while the local row is frozen.
    """

    @pytest.fixture
    def online_pending(self, tier: MembershipTier, subscriber: RevelUser) -> MembershipSubscription:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online Pending",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_cancel",
            stripe_price_id="price_cancel",
        )
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_checkout_session_id="cs_still_open",
        )

    def test_expires_open_session_before_terminalizing(self, online_pending: MembershipSubscription) -> None:
        with mock.patch("stripe.checkout.Session.expire") as mock_expire:
            cancelled = subscription_lifecycle.cancel_subscription(online_pending, immediate=True)

        mock_expire.assert_called_once()
        assert mock_expire.call_args.args[0] == "cs_still_open"
        assert cancelled.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_aborts_when_session_turned_out_complete(self, online_pending: MembershipSubscription) -> None:
        """Expire rejected + the session reads ``complete``: the member paid mid-cancel."""
        with (
            mock.patch(
                "stripe.checkout.Session.expire",
                side_effect=stripe.error.InvalidRequestError("not in status open", "session"),
            ),
            mock.patch("stripe.checkout.Session.retrieve", return_value={"status": "complete"}),
            pytest.raises(SubscriptionActivationPendingError),
        ):
            subscription_lifecycle.cancel_subscription(online_pending, immediate=True)

        online_pending.refresh_from_db()
        assert online_pending.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert online_pending.cancelled_at is None

    def test_already_expired_session_lets_the_cancel_through(self, online_pending: MembershipSubscription) -> None:
        with (
            mock.patch(
                "stripe.checkout.Session.expire",
                side_effect=stripe.error.InvalidRequestError("not in status open", "session"),
            ),
            mock.patch("stripe.checkout.Session.retrieve", return_value={"status": "expired"}),
        ):
            cancelled = subscription_lifecycle.cancel_subscription(online_pending, immediate=True)

        assert cancelled.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_transient_stripe_failure_aborts_with_502(self, online_pending: MembershipSubscription) -> None:
        with (
            mock.patch("stripe.checkout.Session.expire", side_effect=stripe.error.APIConnectionError("boom")),
            pytest.raises(HttpError) as exc,
        ):
            subscription_lifecycle.cancel_subscription(online_pending, immediate=True)

        assert exc.value.status_code == 502
        online_pending.refresh_from_db()
        assert online_pending.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_offline_row_never_calls_stripe(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        with mock.patch("stripe.checkout.Session.expire") as mock_expire:
            cancelled = subscription_lifecycle.cancel_subscription(sub, immediate=True)

        mock_expire.assert_not_called()
        assert cancelled.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_staff_service_path_still_swaps_offline_plans(
        self, plan: MembershipSubscriptionPlan, tier: MembershipTier, subscriber: RevelUser
    ) -> None:
        """The member-endpoint OFFLINE refusal is a controller guard, not a service one."""
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        annual = subscription_plans.create_plan(
            tier, name="Annual offline", price=Decimal("100.00"), currency="EUR", period_unit="year"
        )
        moved = subscription_lifecycle.change_plan(sub, annual, enforce_sales_status=False)
        assert moved.plan_id == annual.pk
