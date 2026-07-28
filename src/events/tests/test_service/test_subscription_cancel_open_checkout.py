"""A *scheduled* cancel of a period-less row must not leave a payable Checkout alive.

Regression for the carve-out that used to exempt rows carrying a
``stripe_checkout_session_id`` from the "no period boundary → cancel now"
upgrade. A PENDING ONLINE row holding a live hosted Checkout took the scheduled
branch: the member got CANCELLATION_CONFIRMED, the row stayed non-terminal with
``cancel_at_period_end=True`` (and kept its ``max_subscriptions`` slot), and the
session stayed payable — so paying it linked the row, ``invoice.paid`` activated
it, and the Stripe sync overwrote ``cancel_at_period_end`` back to False with no
notification at all.
"""

from decimal import Decimal
from unittest import mock

import pytest
import stripe

from accounts.models import RevelUser
from events.exceptions import SubscriptionActivationPendingError
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="cancel_open_user", email="cancel_open@example.com", password="pass"
    )


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A capped ONLINE plan, so the freed cap slot is observable."""
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Online capped",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_open_checkout",
        stripe_price_id="price_open_checkout",
        max_subscriptions=1,
    )


@pytest.fixture
def pending_with_open_session(
    online_plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> MembershipSubscription:
    """PENDING ONLINE row mid-checkout: session minted, no Stripe Subscription yet."""
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=online_plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_live_open",
    )


@pytest.mark.django_db
class TestScheduledCancelOfPendingCheckout:
    def test_scheduled_cancel_expires_session_and_terminalizes(
        self,
        pending_with_open_session: MembershipSubscription,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """``immediate=False`` on a period-less row with a live session cancels now."""
        with mock.patch("stripe.checkout.Session.expire") as mock_expire:
            out = subscription_service.cancel_subscription(pending_with_open_session, immediate=False)

        mock_expire.assert_called_once()
        assert mock_expire.call_args.args[0] == "cs_live_open"
        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert out.cancelled_at is not None
        assert out.cancel_at_period_end is False
        # The row no longer holds the plan's single cap slot.
        assert online_plan.occupied_slot_count() == 0

    def test_scheduled_cancel_notifies_as_immediate(
        self,
        pending_with_open_session: MembershipSubscription,
    ) -> None:
        """The member must be told the subscription ended now, not at a boundary."""
        with (
            mock.patch("stripe.checkout.Session.expire"),
            mock.patch("events.service.subscription_service._dispatch_cancellation_confirmed") as mock_dispatch,
        ):
            subscription_service.cancel_subscription(pending_with_open_session, immediate=False)

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["immediate"] is True

    def test_pay_after_cancel_interleaving_is_refused(
        self,
        pending_with_open_session: MembershipSubscription,
    ) -> None:
        """Session completed mid-cancel → 409, and the row is left alone.

        This is the interleaving the old carve-out silently allowed: the member
        pays the still-open session after a "cancellation confirmed". Now the
        cancel aborts instead, so the activation webhooks settle a row that was
        never falsely reported as cancelled.
        """
        with (
            mock.patch(
                "stripe.checkout.Session.expire",
                side_effect=stripe.error.InvalidRequestError("not in status open", "session"),
            ),
            mock.patch("stripe.checkout.Session.retrieve", return_value={"status": "complete"}),
            pytest.raises(SubscriptionActivationPendingError),
        ):
            subscription_service.cancel_subscription(pending_with_open_session, immediate=False)

        pending_with_open_session.refresh_from_db()
        assert pending_with_open_session.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert pending_with_open_session.cancel_at_period_end is False
        assert pending_with_open_session.cancelled_at is None

    def test_linked_stripe_subscription_still_schedules(
        self,
        pending_with_open_session: MembershipSubscription,
    ) -> None:
        """A paid-but-not-yet-mirrored row keeps the boundary Stripe owns.

        ``stripe_subscription_id`` is only set once the session completed, so
        the member has paid for a period ``invoice.paid`` has not written yet.
        Upgrading that to an immediate cancel would throw the paid period away.
        """
        pending_with_open_session.stripe_subscription_id = "sub_linked_not_mirrored"
        pending_with_open_session.save(update_fields=["stripe_subscription_id"])

        with (
            mock.patch("stripe.checkout.Session.expire") as mock_expire,
            mock.patch("stripe.Subscription.modify") as mock_modify,
        ):
            out = subscription_service.cancel_subscription(pending_with_open_session, immediate=False)

        mock_expire.assert_not_called()
        assert mock_modify.call_args.kwargs["cancel_at_period_end"] is True
        assert out.cancel_at_period_end is True
        assert out.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_offline_period_less_row_still_cancels_immediately(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """Unchanged behaviour: a staff-created OFFLINE PENDING row has nothing to wait for."""
        offline_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Offline monthly",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        sub = subscription_service.create_subscription(offline_plan, subscriber)

        out = subscription_service.cancel_subscription(sub, immediate=False)

        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert out.cancel_at_period_end is False
