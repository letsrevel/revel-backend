"""Tests for the ``checkout.session.completed`` → subscription link routing.

Hosted-Checkout subscriptions (mode=subscription) have no Payment/Ticket rows;
the handler links ``session.subscription`` onto the local PENDING row via
``metadata.membership_subscription_id`` without disturbing the ticket path.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


def _session_event(session: dict[str, t.Any]) -> MagicMock:
    """Build a mock checkout.session.completed event around ``session``."""
    event_data = {"id": "evt_sub_checkout", "type": "checkout.session.completed", "data": {"object": session}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = session
    return mock_event


@pytest.fixture
def online_plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
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
        username="checkout_subscriber", email="checkout-sub@example.com", password="pass"
    )


@pytest.fixture
def pending_subscription(
    online_plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=online_plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_sub_test",
    )


class TestSubscriptionCheckoutCompleted:
    def test_links_stripe_subscription_id(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_from_session",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        handler = StripeEventHandler(_session_event(session))

        assert handler.handle() is True

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_from_session"
        # Status stays PENDING — invoice.paid flips it to ACTIVE downstream.
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_links_expanded_subscription_object(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": {"id": "sub_expanded"},
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_expanded"

    def test_redelivery_is_idempotent(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_once",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_once"

    def test_subscription_mode_without_metadata_is_ignored(self, pending_subscription: MembershipSubscription) -> None:
        """Orgs can run their own subscription checkouts on the same Connect account."""
        session = {
            "id": "cs_foreign",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_foreign",
            "metadata": {},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id is None

    def test_unknown_local_row_is_tolerated(self) -> None:
        session = {
            "id": "cs_orphan",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_orphan",
            "metadata": {"membership_subscription_id": "00000000-0000-0000-0000-000000000000"},
        }
        # Must not raise — a 500 would wedge Stripe redelivery.
        StripeEventHandler(_session_event(session)).handle()

    def test_malformed_metadata_is_tolerated(self) -> None:
        session = {
            "id": "cs_bad_meta",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_bad",
            "metadata": {"membership_subscription_id": "not-a-uuid"},
        }
        StripeEventHandler(_session_event(session)).handle()

    def test_missing_subscription_reference_is_tolerated(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": None,
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id is None

    def test_metadata_only_routing_without_mode(self, pending_subscription: MembershipSubscription) -> None:
        """A session carrying our metadata key routes to the subscription handler even without mode."""
        session = {
            "id": "cs_sub_test",
            "payment_status": "paid",
            "subscription": "sub_meta_routed",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_meta_routed"
