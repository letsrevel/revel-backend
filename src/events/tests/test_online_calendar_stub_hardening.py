"""ONLINE calendar plans, part 3: the free-stub decision timing, stamp resolution and first-invoice shapes (#1426).

Shares fixtures and payload builders with ``test_online_calendar_plans``.
"""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription.stripe import calendar_stub as stub_module
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync
from events.tests import test_online_calendar_free_stub as free_stub
from events.tests import test_online_calendar_plans as base
from events.utils.subscription_periods import first_period_price, is_free_stub

pytestmark = pytest.mark.django_db

CREATE_SESSION, UTC, Plan = base.CREATE_SESSION, base.UTC, base.Plan
_first_invoice, _online_plan, _renewal_invoice, _session = (
    base._first_invoice,
    base._online_plan,
    base._renewal_invoice,
    base._session,
)

stripe_org = base.stripe_org
tier = base.tier
calendar_year_plan = base.calendar_year_plan
rolling_plan = base.rolling_plan
subscriber = base.subscriber
pending_calendar_sub = base.pending_calendar_sub
stripe_price_retrieve = base.stripe_price_retrieve


def _monthly(tier: MembershipTier, price: str = "120.00") -> MembershipSubscriptionPlan:
    return _online_plan(
        tier,
        price=Decimal(price),
        period_unit=Plan.PeriodUnit.MONTH,
        period_alignment=Plan.PeriodAlignment.CALENDAR,
    )


class TestFreeStubIsDecidedAtTheLatestCompletion:
    """Stripe prorates at completion, up to the session's ``expires_at``; the stub is free if it would be then."""

    def test_stub_chargeable_now_but_not_at_the_latest_completion_is_free(self, tier: MembershipTier) -> None:
        # 70 minutes left, 45 minute session: completing at the end of it leaves 25 minutes (< 1 hour).
        plan = _monthly(tier, "100000.00")
        at = datetime.datetime(2026, 11, 30, 22, 50, tzinfo=UTC)
        assert is_free_stub(plan, at)
        assert first_period_price(plan, at) == Decimal("0.00")

    def test_below_the_minimum_charge_at_the_latest_completion_is_free(self, tier: MembershipTier) -> None:
        # 12:00 + 45 min on a 30.00 plan: 30 x 11.25h/720h = 0.47 EUR < 0.50 although 0.50 at 12:00.
        assert is_free_stub(_monthly(tier, "30.00"), datetime.datetime(2026, 11, 30, 12, tzinfo=UTC))

    def test_comfortably_chargeable_stub_is_still_prorated(self, tier: MembershipTier) -> None:
        plan = _monthly(tier)
        at = datetime.datetime(2026, 11, 15, 12, tzinfo=UTC)
        assert not is_free_stub(plan, at)
        assert first_period_price(plan, at) == Decimal("62.00")

    @freeze_time("2026-11-30 22:50:00")
    def test_session_whose_completion_could_land_inside_the_last_hour_does_not_prorate(
        self, tier: MembershipTier, subscriber: RevelUser
    ) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(_monthly(tier, "100000.00"), subscriber)

        assert create.call_args.kwargs["subscription_data"]["proration_behavior"] == "none"


class TestStampResolvesTheLinesPrice:
    def test_zero_line_amount_is_never_taken_as_the_price(
        self, pending_calendar_sub: MembershipSubscription, stripe_price_retrieve: mock.MagicMock
    ) -> None:
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"]["unit_amount_decimal"] = "0"

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")  # the line's Price is the plan's: plan.price
        stripe_price_retrieve.assert_not_called()

    def test_prorated_line_amount_is_never_taken_as_the_price(
        self, pending_calendar_sub: MembershipSubscription, stripe_price_retrieve: mock.MagicMock
    ) -> None:
        stripe_price_retrieve.return_value = {"unit_amount": 9900}
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"] = {
            "price_details": {"price": "price_grandfathered"},
            "unit_amount_decimal": "1433",
        }

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("99")
        assert stripe_price_retrieve.call_args.args[0] == "price_grandfathered"
        assert stripe_price_retrieve.call_args.kwargs == {"stripe_account": "acct_test_org"}

    def test_line_without_a_price_id_falls_back_to_plan_price(
        self, pending_calendar_sub: MembershipSubscription, stripe_price_retrieve: mock.MagicMock
    ) -> None:
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"] = {}

        with mock.patch.object(stub_module.logger, "warning") as log_warning:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")  # plan.price, never the stub amount
        stripe_price_retrieve.assert_not_called()
        assert log_warning.call_args.args[0] == "subscription_stub_line_without_price_using_plan_price"

    @pytest.mark.parametrize(
        "failure", [stripe.error.APIConnectionError("boom"), None], ids=["stripe-error", "no-unit"]
    )
    def test_unreadable_price_still_activates_and_stamps_the_plan_price(
        self,
        pending_calendar_sub: MembershipSubscription,
        stripe_price_retrieve: mock.MagicMock,
        failure: Exception | None,
    ) -> None:
        """invoice.paid: a paying member is never stranded over the best-effort anchor."""
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"] = {"price_details": {"price": "price_grandfathered"}}
        if failure is None:
            stripe_price_retrieve.return_value = {"unit_amount": None}
        else:
            stripe_price_retrieve.side_effect = failure

        with mock.patch.object(stub_module.logger, "warning") as log_warning:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")  # plan.price fallback
        assert payment.amount == Decimal("14.33")
        pending_calendar_sub.refresh_from_db()
        assert pending_calendar_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert OrganizationMember.objects.filter(user=pending_calendar_sub.user).exists()
        kwargs = log_warning.call_args.kwargs
        assert (kwargs["subscription_id"], kwargs["stripe_invoice_id"], kwargs["stripe_price_id"]) == (
            str(pending_calendar_sub.pk),
            "in_first",
            "price_grandfathered",
        )
        assert "email" not in kwargs

    def test_unreadable_price_on_checkout_completion_still_links_and_activates(
        self,
        calendar_year_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
        stripe_price_retrieve: mock.MagicMock,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=calendar_year_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_checkout_session_id="cs_cal",
        )
        stripe_price_retrieve.side_effect = stripe.error.APIConnectionError("boom")
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"] = {"price_details": {"price": "price_grandfathered"}}
        handler = free_stub._completion_handler(sub, invoice="in_first")

        with mock.patch(free_stub.RETRIEVE_INVOICE, return_value={**invoice, "status": "paid"}):
            handler.handle_subscription_checkout_completed(handler.event)

        sub.refresh_from_db()
        assert sub.stripe_subscription_id == "sub_cal"
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        payment = MembershipPayment.objects.get(subscription=sub)
        assert payment.full_period_amount == Decimal("120.00")

    def test_stub_decision_is_retaken_under_the_row_lock(
        self, pending_calendar_sub: MembershipSubscription, stripe_price_retrieve: mock.MagicMock
    ) -> None:
        """The plan stopped being a calendar plan between the unlocked read and the lock: no stamp."""

        def swap_plan(*args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
            MembershipSubscriptionPlan.objects.filter(pk=pending_calendar_sub.plan_id).update(
                period_alignment=Plan.PeriodAlignment.ROLLING
            )
            return {"unit_amount": 9900}

        stripe_price_retrieve.side_effect = swap_plan
        invoice = _first_invoice()
        invoice["lines"]["data"][0]["pricing"] = {"price_details": {"price": "price_grandfathered"}}

        with mock.patch.object(stub_module.logger, "warning"):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        stripe_price_retrieve.assert_called_once()
        assert payment.full_period_amount is None


class TestFirstInvoiceShapes:
    def test_full_period_covered_by_customer_credit_is_not_a_stub(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        """amount_paid 0 only because a credit balance absorbed a full-price invoice (total > 0, no proration)."""
        invoice = _renewal_invoice()
        invoice.update(id="in_credit", billing_reason="subscription_create", amount_paid=0, amount_due=0, total=12000)
        invoice["payments"] = {"data": []}

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount is None

    def test_zero_total_first_invoice_is_the_free_stub(self, pending_calendar_sub: MembershipSubscription) -> None:
        invoice = _first_invoice(invoice_id="in_free0", amount=0)
        invoice.update(total=0, subtotal=0, payments={"data": []})
        invoice["lines"]["data"][0].update(amount=0, parent={})  # a zero line on the plan's Price, no proration flag

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")
