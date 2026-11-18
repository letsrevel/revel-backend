"""ONLINE calendar plans, part 3: the free-stub decision timing, stamp resolution and first-invoice shapes (#1426).

Shares fixtures and payload builders with ``test_online_calendar_plans``.
"""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.template.loader import render_to_string
from django.utils import translation
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync
from events.tests import test_online_calendar_plans as base
from events.utils.subscription_periods import first_period_price, is_free_stub
from notifications.enums import NotificationType
from notifications.models import Notification

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

    @pytest.mark.parametrize(
        "failure",
        [
            stripe.error.APIConnectionError("boom"),
            None,  # a Price with no integer unit_amount (tiered/metered)
        ],
        ids=["stripe-error", "no-unit-amount"],
    )
    def test_unresolvable_price_fails_the_webhook_and_writes_nothing(
        self,
        pending_calendar_sub: MembershipSubscription,
        stripe_price_retrieve: mock.MagicMock,
        failure: Exception | None,
    ) -> None:
        pending_calendar_sub.plan.stripe_price_id = "price_new"
        pending_calendar_sub.plan.save(update_fields=["stripe_price_id"])
        if failure is None:
            stripe_price_retrieve.return_value = {"unit_amount": None}
        else:
            stripe_price_retrieve.side_effect = failure

        with pytest.raises((stripe.error.StripeError, ValueError)):
            subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        assert not MembershipPayment.objects.filter(subscription=pending_calendar_sub).exists()
        pending_calendar_sub.refresh_from_db()
        assert pending_calendar_sub.status == MembershipSubscription.SubscriptionStatus.PENDING

        # Stripe redelivers: with the Price readable again the stamp lands.
        stripe_price_retrieve.side_effect = None
        stripe_price_retrieve.return_value = {"unit_amount": 12000}
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)
        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")


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
        invoice["lines"]["data"] = []

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")


class TestNewMemberGetsTheNextChargeLine:
    def test_membership_granted_states_the_next_charge_for_a_calendar_first_payment(
        self,
        pending_calendar_sub: MembershipSubscription,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        context = Notification.objects.get(
            user=pending_calendar_sub.user, notification_type=NotificationType.MEMBERSHIP_GRANTED
        ).context
        assert "120" in context["next_charge_amount"]
        assert context["next_charge_date"] == "2027-01-01"

    def test_membership_granted_for_a_rolling_plan_has_no_next_charge(
        self,
        rolling_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=rolling_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_cal",
        )
        invoice = _renewal_invoice()
        invoice.update(id="in_roll", billing_reason="subscription_create")
        with django_capture_on_commit_callbacks(execute=True):
            subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        context = Notification.objects.get(user=sub.user, notification_type=NotificationType.MEMBERSHIP_GRANTED).context
        assert "next_charge_amount" not in context

    @pytest.mark.parametrize("ext", ["email/{}.txt", "email/{}.html", "in_app/{}.md", "telegram/{}.md"])
    def test_template_renders_the_line_only_when_present(self, ext: str) -> None:
        name = f"notifications/{ext.format('membership_granted')}"
        ctx = {"organization_name": "O", "role": "member", "frontend_url": "https://x.example"}
        with translation.override("en"):
            plain = render_to_string(name, {"user": mock.Mock(display_name="U"), "context": ctx})
            noticed = render_to_string(
                name,
                {
                    "user": mock.Mock(display_name="U"),
                    "context": {**ctx, "next_charge_amount": "120.00 EUR", "next_charge_date": "2027-01-01"},
                },
            )

        assert "Next charge" not in plain
        assert "Next charge: 120.00 EUR on 2027-01-01." in noticed
