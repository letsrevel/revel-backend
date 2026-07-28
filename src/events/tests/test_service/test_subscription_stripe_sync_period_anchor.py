"""Regression tests: the sync path's billing anchor only ever moves forward.

``customer.subscription.*`` and the nightly reconcile are the *primary* writers of
``current_period_{start,end}``, and webhook dedup is by Stripe event id only. A
redelivered ``customer.subscription.updated`` from the previous cycle — Stripe
retries for up to 3 days — used to rewind an anchor that ``invoice.paid`` had
already advanced, which silently disarms the refund auto-cancel
(``_is_full_refund_of_current_period`` matches on the live anchor), misfires the
renewal reminder, and lets the grace-expiry beat flip a paid-up member to PAST_DUE.

The invoice path got this guard in the #774 review; these pin the same invariant on
the sync path.
"""

import datetime
import time
import typing as t
from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_refunds, subscription_stripe_sync

pytestmark = pytest.mark.django_db

STRIPE_SUB_ID = "sub_anchor"


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
        stripe_product_id="prod_anchor",
        stripe_price_id="price_anchor",
    )


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="anchor_member", email="anchor-member@example.com", password="pass"
    )


def _dt(epoch: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)


def _payload(
    period_start: int | None,
    period_end: int | None,
    *,
    status: str = "active",
    cancel_at_period_end: bool = False,
) -> dict[str, t.Any]:
    """A dahlia-shaped ``customer.subscription.updated`` body (period on the item)."""
    item: dict[str, t.Any] = {"id": "si_anchor", "price": {"id": "price_anchor"}}
    if period_start is not None:
        item["current_period_start"] = period_start
    if period_end is not None:
        item["current_period_end"] = period_end
    return {
        "id": STRIPE_SUB_ID,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "items": {"data": [item]},
    }


class TestSyncPeriodAnchorIsForwardOnly:
    @staticmethod
    def _active_sub(
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        *,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id=STRIPE_SUB_ID,
            current_period_start=start,
            current_period_end=end,
        )

    def test_stale_updated_does_not_rewind_the_anchor(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """The core case: a retried event from the previous cycle lands after the renewal."""
        now = int(time.time())
        current_start, current_end = _dt(now - 5 * 86400), _dt(now + 25 * 86400)
        sub = self._active_sub(online_plan, member_user, start=current_start, end=current_end)

        subscription_stripe_sync.sync_subscription_from_stripe(
            _payload(now - 35 * 86400, now - 5 * 86400),
        )

        sub.refresh_from_db()
        assert sub.current_period_start == current_start
        assert sub.current_period_end == current_end

    def test_a_period_only_rewind_writes_nothing_at_all(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """``update_fields`` must come back empty — no save, so ``updated_at`` stands still."""
        now = int(time.time())
        sub = self._active_sub(online_plan, member_user, start=_dt(now - 5 * 86400), end=_dt(now + 25 * 86400))
        untouched_updated_at = sub.updated_at

        subscription_stripe_sync.sync_subscription_from_stripe(
            _payload(now - 35 * 86400, now - 5 * 86400),
        )

        sub.refresh_from_db()
        assert sub.updated_at == untouched_updated_at

    def test_a_stale_period_does_not_swallow_the_rest_of_the_payload(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """Only the period is dropped: a status/cap change on the same event still lands."""
        now = int(time.time())
        current_start, current_end = _dt(now - 5 * 86400), _dt(now + 25 * 86400)
        sub = self._active_sub(online_plan, member_user, start=current_start, end=current_end)

        subscription_stripe_sync.sync_subscription_from_stripe(
            _payload(now - 35 * 86400, now - 5 * 86400, status="past_due", cancel_at_period_end=True),
        )

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        assert sub.cancel_at_period_end is True
        assert sub.current_period_start == current_start
        assert sub.current_period_end == current_end

    def test_a_forward_move_still_applies(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """The renewal case — the whole point of the sync path — is untouched."""
        now = int(time.time())
        sub = self._active_sub(online_plan, member_user, start=_dt(now - 30 * 86400), end=_dt(now))
        next_start, next_end = now, now + 30 * 86400

        subscription_stripe_sync.sync_subscription_from_stripe(_payload(next_start, next_end))

        sub.refresh_from_db()
        assert sub.current_period_start == _dt(next_start)
        assert sub.current_period_end == _dt(next_end)

    def test_initial_link_of_a_pending_row_applies_any_period(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """No local anchor yet: there is nothing to rewind, so the payload wins outright.

        The period offered here is entirely in the past (a revival row whose Stripe
        subscription was created against an elapsed cycle) — the guard must not
        mistake "no anchor" for "a floor of zero".
        """
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id=STRIPE_SUB_ID,
        )
        assert sub.current_period_start is None
        now = int(time.time())

        subscription_stripe_sync.sync_subscription_from_stripe(_payload(now - 40 * 86400, now - 10 * 86400))

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_start == _dt(now - 40 * 86400)
        assert sub.current_period_end == _dt(now - 10 * 86400)

    def test_a_periodless_payload_leaves_the_anchor_alone(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """A pause/resume echo carries no period; the guard must not blank the anchor."""
        now = int(time.time())
        current_start, current_end = _dt(now - 5 * 86400), _dt(now + 25 * 86400)
        sub = self._active_sub(online_plan, member_user, start=current_start, end=current_end)

        subscription_stripe_sync.sync_subscription_from_stripe(_payload(None, None))

        sub.refresh_from_db()
        assert sub.current_period_start == current_start
        assert sub.current_period_end == current_end

    def test_refund_auto_cancel_still_matches_after_a_stale_event(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """The consequence the guard exists for.

        ``_is_full_refund_of_current_period`` matches the payment's ``period_start``
        against the *live* anchor. A rewound anchor makes the just-paid period look
        historical, the auto-cancel silently no-ops, and the member keeps both the
        refund and the membership — while Stripe keeps billing them.
        """
        now = int(time.time())
        current_start, current_end = _dt(now - 5 * 86400), _dt(now + 25 * 86400)
        sub = self._active_sub(online_plan, member_user, start=current_start, end=current_end)
        payment = MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.REFUNDED,
            period_start=current_start,
            period_end=current_end,
            stripe_invoice_id="in_anchor",
        )

        subscription_stripe_sync.sync_subscription_from_stripe(
            _payload(now - 35 * 86400, now - 5 * 86400),
        )

        # Re-read so the cached ``payment.subscription`` reflects the post-sync anchor.
        payment = MembershipPayment.objects.select_related("subscription").get(pk=payment.pk)
        assert subscription_refunds._is_full_refund_of_current_period(payment) is True
