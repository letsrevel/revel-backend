"""Tests for the stale-PENDING sweep's Stripe observation step.

Clearing an ONLINE PENDING row on age alone is unsafe: if
``checkout.session.completed`` kept failing (each redelivery rolls the link
back), the Checkout Session can be ``complete`` with money captured while the
row still looks like an abandoned redirect. Deleting it there makes the payment
permanently undiscoverable — later webhooks match no row, and the reconcile pass
walks local rows — so the sweep retrieves the session first and only clears rows
Stripe confirms are dead.
"""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.tasks.subscriptions import reconcile_stripe_subscriptions

pytestmark = pytest.mark.django_db

SESSION_RETRIEVE = "events.service.subscription_stripe_service.stripe.checkout.Session.retrieve"


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="sweep_user", email="sweep@example.com", password="pass")


@pytest.fixture
def stale_pending(tier: MembershipTier, organization: Organization, subscriber: RevelUser) -> MembershipSubscription:
    """An ONLINE PENDING row untouched for two days — a sweep candidate."""
    online_plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Online sweep",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_price_id="price_sweep",
        stripe_product_id="prod_sweep",
    )
    sub = MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_subscription_id="",
    )
    MembershipSubscription.objects.filter(pk=sub.pk).update(
        stripe_checkout_session_id="cs_sweep",
        updated_at=timezone.now() - datetime.timedelta(days=2),
    )
    sub.refresh_from_db()
    return sub


def _session(status: str) -> t.Any:
    return mock.MagicMock(id="cs_sweep", status=status)


class TestStalePendingSweepObservesStripe:
    def test_complete_session_keeps_the_row_and_raises_an_incident(self, stale_pending: MembershipSubscription) -> None:
        """A paid-but-unlinked checkout must survive the sweep.

        The row is the only handle back to the captured money: deleting it leaves
        the member charged with no membership and no ledger entry, and nothing
        downstream can rediscover it. Instead it stays put and an incident is
        raised so a human can link or refund it.
        """
        with (
            mock.patch(SESSION_RETRIEVE, return_value=_session("complete")) as retrieve,
            mock.patch("events.service.stripe_incidents.record_subscription_checkout_paid_but_unlinked") as incident,
        ):
            counters = reconcile_stripe_subscriptions()

        retrieve.assert_called_once()
        assert retrieve.call_args.args[0] == "cs_sweep"
        assert counters["stale_pending_cleared"] == 0
        assert MembershipSubscription.objects.filter(pk=stale_pending.pk).exists()
        stale_pending.refresh_from_db()
        assert stale_pending.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert stale_pending.stripe_checkout_session_id == "cs_sweep"
        incident.assert_called_once_with(
            subscription_id=str(stale_pending.pk),
            organization_id=str(stale_pending.organization_id),
            user_id=str(stale_pending.user_id),
            session_id="cs_sweep",
        )

    def test_expired_session_is_cleared_as_before(self, stale_pending: MembershipSubscription) -> None:
        """Stripe confirming the session expired keeps the existing sweep behaviour."""
        with mock.patch(SESSION_RETRIEVE, return_value=_session("expired")):
            counters = reconcile_stripe_subscriptions()

        assert counters["stale_pending_cleared"] == 1
        assert not MembershipSubscription.objects.filter(pk=stale_pending.pk).exists()

    def test_retrieve_failure_skips_the_row(self, stale_pending: MembershipSubscription) -> None:
        """A Stripe outage must not be read as "abandoned" — retry tomorrow."""
        with mock.patch(SESSION_RETRIEVE, side_effect=stripe.error.APIConnectionError("down")):
            counters = reconcile_stripe_subscriptions()

        assert counters["stale_pending_cleared"] == 0
        stale_pending.refresh_from_db()
        assert stale_pending.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_still_open_session_is_left_for_the_next_run(self, stale_pending: MembershipSubscription) -> None:
        """An open session carries an ``expires_at`` and dies on its own; clearing
        the row while its URL is still payable is the same money-loss hazard."""
        with mock.patch(SESSION_RETRIEVE, return_value=_session("open")):
            counters = reconcile_stripe_subscriptions()

        assert counters["stale_pending_cleared"] == 0
        assert MembershipSubscription.objects.filter(pk=stale_pending.pk).exists()

    def test_row_without_a_session_id_is_cleared_without_calling_stripe(
        self, stale_pending: MembershipSubscription
    ) -> None:
        """A stranded row from a failed session-create has nothing payable behind it."""
        MembershipSubscription.objects.filter(pk=stale_pending.pk).update(
            stripe_checkout_session_id="",
            updated_at=timezone.now() - datetime.timedelta(days=2),
        )
        with mock.patch(SESSION_RETRIEVE) as retrieve:
            counters = reconcile_stripe_subscriptions()

        retrieve.assert_not_called()
        assert counters["stale_pending_cleared"] == 1
        assert not MembershipSubscription.objects.filter(pk=stale_pending.pk).exists()
