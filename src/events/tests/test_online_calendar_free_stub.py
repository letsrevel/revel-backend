"""ONLINE calendar plans, part 2: the free tiny stub, notices, sync ordering and the invoice stamp (#1426).

Shares fixtures and payload builders with ``test_online_calendar_plans`` (split to stay under the
file-length cap).
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
    OrganizationMember,
)
from events.service.stripe_webhooks import StripeEventHandler
from events.service.subscription import notifications as subscription_notifications
from events.service.subscription import refunds as subscription_refunds
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync
from events.tests import test_online_calendar_plans as base
from events.utils.subscription_periods import first_period_price, is_calendar_plan, is_free_stub
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

CREATE_SESSION, JOIN, BOUNDARY, UTC, Plan = base.CREATE_SESSION, base.JOIN, base.BOUNDARY, base.UTC, base.Plan
_epoch, _first_invoice, _online_plan = base._epoch, base._first_invoice, base._online_plan
_renewal_invoice, _session, _subscription_payload = base._renewal_invoice, base._session, base._subscription_payload

# Fixtures are shared by assignment: pytest registers them under the attribute name.
stripe_org = base.stripe_org
tier = base.tier
calendar_year_plan = base.calendar_year_plan
rolling_plan = base.rolling_plan
subscriber = base.subscriber
pending_calendar_sub = base.pending_calendar_sub
stripe_price_retrieve = base.stripe_price_retrieve


# ---- Free tiny stub ------------------------------------------------------------------------------


def _monthly(tier: MembershipTier, price: str = "120.00", **overrides: t.Any) -> MembershipSubscriptionPlan:
    return _online_plan(
        tier,
        price=Decimal(price),
        period_unit=Plan.PeriodUnit.MONTH,
        period_alignment=Plan.PeriodAlignment.CALENDAR,
        **overrides,
    )


class TestFreeStubRule:
    @pytest.mark.parametrize(
        ("price", "at", "free"),
        [
            ("120.00", "2026-11-30 23:30:00", True),  # 30 minutes left: under the hour
            ("120.00", "2026-11-30 23:00:01", True),  # just under the hour
            ("120.00", "2026-11-30 12:00:00", False),  # 12h of 120/30d = 2.00 EUR, chargeable
            ("1.00", "2026-11-29 12:00:00", True),  # 1.00 x 1.5/30 = 0.05 EUR, below Stripe's 0.50
            ("20.00", "2026-11-30 12:00:00", True),  # 20 x 12h/720h = 0.33 EUR, below the minimum
        ],
    )
    def test_rule(self, tier: MembershipTier, price: str, at: str, free: bool) -> None:
        moment = datetime.datetime.fromisoformat(at).replace(tzinfo=UTC)
        assert is_free_stub(_monthly(tier, price), moment) is free

    def test_exactly_the_minimum_charge_is_billed(self, tier: MembershipTier) -> None:
        # Decided at the session's latest completion (12:45): 32 x 11.25h/720h = 0.50 EUR; quoted at 12:00.
        plan = _monthly(tier, "32.00")
        assert first_period_price(plan, datetime.datetime(2026, 11, 30, 12, tzinfo=UTC)) == Decimal("0.53")
        assert not is_free_stub(plan, datetime.datetime(2026, 11, 30, 12, tzinfo=UTC))

    def test_currency_minimum_applies(self, tier: MembershipTier) -> None:
        """CZK's minimum charge is 15.00: a 5.00 CZK stub is free where 5.00 EUR is not."""
        at = datetime.datetime(2026, 11, 15, tzinfo=UTC)  # half the month left
        assert is_free_stub(_monthly(tier, "10.00", currency="CZK", name="Czk"), at)
        assert not is_free_stub(_monthly(tier, "10.00", currency="EUR", name="Eur", stripe_price_id="price_eur"), at)

    def test_quote_is_zero_for_a_free_stub_and_prorated_otherwise(self, tier: MembershipTier) -> None:
        plan = _monthly(tier)
        assert first_period_price(plan, datetime.datetime(2026, 11, 30, 23, 30, tzinfo=UTC)) == Decimal("0.00")
        assert first_period_price(plan, datetime.datetime(2026, 11, 15, tzinfo=UTC)) == Decimal(
            "64.00"
        )  # 16 of 30 days left

    def test_offline_plans_never_get_a_free_stub(self, tier: MembershipTier) -> None:
        plan = _monthly(tier, "120.00", payment_method=Plan.PaymentMethod.OFFLINE, stripe_price_id="")
        at = datetime.datetime(2026, 11, 30, 23, 30, tzinfo=UTC)
        assert not is_free_stub(plan, at)
        assert first_period_price(plan, at) == Decimal("0.08")

    def test_rolling_plans_have_no_stub(self, rolling_plan: MembershipSubscriptionPlan) -> None:
        assert not is_calendar_plan(rolling_plan)
        assert not is_free_stub(rolling_plan, JOIN)


class TestFreeStubCheckout:
    @freeze_time("2026-11-30 23:50:00")
    def test_session_minted_under_an_hour_before_the_boundary_does_not_prorate(
        self, tier: MembershipTier, subscriber: RevelUser
    ) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(_monthly(tier), subscriber)

        data = create.call_args.kwargs["subscription_data"]
        assert data["proration_behavior"] == "none"
        assert data["billing_cycle_anchor"] == _epoch(datetime.datetime(2026, 12, 1, tzinfo=UTC))

    @freeze_time("2026-11-29 12:00:00")
    def test_stub_below_the_minimum_charge_does_not_prorate(self, tier: MembershipTier, subscriber: RevelUser) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(_monthly(tier, "1.00"), subscriber)

        assert create.call_args.kwargs["subscription_data"]["proration_behavior"] == "none"

    @freeze_time("2026-11-30 12:00:00")
    def test_stub_over_an_hour_and_chargeable_still_prorates(self, tier: MembershipTier, subscriber: RevelUser) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(_monthly(tier, "100000.00"), subscriber)

        assert create.call_args.kwargs["subscription_data"]["proration_behavior"] == "create_prorations"


# ---- Quoted amounts and notices ------------------------------------------------------------------


class TestStubNotices:
    @freeze_time(JOIN)
    def test_revival_link_states_the_next_full_charge_and_its_date(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=calendar_year_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_notifications._dispatch_revival_checkout(sub, checkout_url="https://checkout.example/x")

        context = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT
        ).context
        assert "120" in context["next_charge_amount"]
        assert context["next_charge_date"] == "2027-01-01"

    @freeze_time("2026-11-30 23:30:00")
    def test_revival_link_for_a_free_stub_quotes_zero_then_the_full_charge(
        self, tier: MembershipTier, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        plan = _monthly(tier)
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_notifications._dispatch_revival_checkout(sub, checkout_url="https://checkout.example/x")

        context = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT
        ).context
        assert context["amount"].startswith("0")
        assert "120" in context["next_charge_amount"]
        assert context["next_charge_date"] == "2026-12-01"

    @freeze_time(JOIN)
    def test_revival_link_on_a_rolling_plan_has_no_next_charge(
        self, rolling_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=rolling_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_notifications._dispatch_revival_checkout(sub, checkout_url="https://checkout.example/x")

        context = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT
        ).context
        assert "next_charge_amount" not in context


# ---- Sync: ordering, free stub, stamp ------------------------------------------------------------


def _free_stub_invoice(*, with_line: bool = True, invoice_id: str = "in_free") -> dict[str, t.Any]:
    """The first invoice of a sub-minimum stub as Stripe returned it in test mode (probe, API dahlia).

    ``total`` 1 cent, ``amount_due``/``amount_paid`` 0, an empty ``payments`` list (no PaymentIntent).
    """
    invoice = _first_invoice(invoice_id=invoice_id, amount=0)
    invoice["payments"] = {"data": []}
    if with_line:
        invoice["lines"]["data"][0]["amount"] = 1
    else:
        invoice.update(total=0, subtotal=0)
        invoice["lines"]["data"] = []
    return invoice


@pytest.fixture
def unlinked_calendar_sub(
    calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
) -> MembershipSubscription:
    """A PENDING row whose Checkout Session has not completed yet (no Stripe Subscription id)."""
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=calendar_year_plan,
        organization=stripe_org,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_cal",
    )


def _completion_handler(sub: MembershipSubscription, *, invoice: str | None) -> StripeEventHandler:
    event = mock.MagicMock(spec=stripe.Event)
    event.type = "checkout.session.completed"
    event.account = sub.organization.stripe_account_id
    event.data = mock.MagicMock()
    event.data.object = {
        "id": "cs_cal",
        "mode": "subscription",
        "subscription": "sub_cal",
        "invoice": invoice,
        "metadata": {"membership_subscription_id": str(sub.pk)},
    }
    return StripeEventHandler(event)


RETRIEVE_INVOICE = "events.service.stripe_webhook_subscriptions.stripe.Invoice.retrieve"
RETRIEVE_SUBSCRIPTION = "events.service.stripe_webhook_subscriptions.stripe.Subscription.retrieve"


class TestEventsBeforeCheckoutCompletion:
    def test_proration_only_first_invoice_still_sets_the_period_via_the_completion_backfill(
        self, unlinked_calendar_sub: MembershipSubscription
    ) -> None:
        """invoice.paid and customer.subscription.* beat checkout.session.completed and were dropped."""
        assert subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True) is None
        assert (
            subscription_stripe_sync.sync_subscription_from_stripe(_subscription_payload(start=JOIN, end=BOUNDARY))
            is None
        )

        handler = _completion_handler(unlinked_calendar_sub, invoice="in_first")
        with mock.patch(RETRIEVE_INVOICE, return_value={**_first_invoice(), "status": "paid"}):
            handler.handle_subscription_checkout_completed(handler.event)

        unlinked_calendar_sub.refresh_from_db()
        assert unlinked_calendar_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert unlinked_calendar_sub.current_period_start == JOIN
        assert unlinked_calendar_sub.current_period_end == BOUNDARY

    def test_same_day_full_refund_after_the_backfill_cancels_the_subscription(
        self, unlinked_calendar_sub: MembershipSubscription
    ) -> None:
        handler = _completion_handler(unlinked_calendar_sub, invoice="in_first")
        with mock.patch(RETRIEVE_INVOICE, return_value={**_first_invoice(), "status": "paid"}):
            handler.handle_subscription_checkout_completed(handler.event)
        payment = MembershipPayment.objects.get(subscription=unlinked_calendar_sub)

        subscription_refunds.refund_payment(payment, recorded_by=None)

        unlinked_calendar_sub.refresh_from_db()
        assert unlinked_calendar_sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_completion_without_an_invoice_mirrors_the_subscription(
        self, unlinked_calendar_sub: MembershipSubscription
    ) -> None:
        """A free stub may open no invoice at all: the Subscription itself activates the row."""
        handler = _completion_handler(unlinked_calendar_sub, invoice=None)
        with mock.patch(RETRIEVE_SUBSCRIPTION, return_value=_subscription_payload(start=JOIN, end=BOUNDARY)):
            handler.handle_subscription_checkout_completed(handler.event)

        unlinked_calendar_sub.refresh_from_db()
        assert unlinked_calendar_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert (unlinked_calendar_sub.current_period_start, unlinked_calendar_sub.current_period_end) == (
            JOIN,
            BOUNDARY,
        )
        assert not MembershipPayment.objects.filter(subscription=unlinked_calendar_sub).exists()


class TestFreeStubInvoice:
    @pytest.mark.parametrize("with_line", [True, False], ids=["tiny-line", "no-lines"])
    def test_zero_first_invoice_records_a_zero_row_without_alarms(
        self, pending_calendar_sub: MembershipSubscription, with_line: bool
    ) -> None:
        with (
            mock.patch("events.service.subscription.stripe.payloads.stripe.Invoice.retrieve") as retrieve,
            mock.patch(
                "events.service.subscription.stripe.sync.stripe_incidents.record_subscription_payment_intent_unresolved"
            ) as intent_alarm,
            mock.patch("events.service.subscription.stripe.sync.logger") as log,
        ):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _free_stub_invoice(with_line=with_line), succeeded=True
            )
            subscription_stripe_sync.sync_subscription_from_stripe(_subscription_payload(start=JOIN, end=BOUNDARY))

        assert payment is not None
        retrieve.assert_not_called()
        intent_alarm.assert_not_called()
        assert "subscription_invoice_application_fee_unresolved" not in [c.args[0] for c in log.warning.call_args_list]
        assert payment.amount == Decimal("0")
        # A line-less invoice names no Price: the stamp is never guessed from plan.price.
        assert payment.full_period_amount == (Decimal("120.00") if with_line else None)
        pending_calendar_sub.refresh_from_db()
        assert pending_calendar_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert (pending_calendar_sub.current_period_start, pending_calendar_sub.current_period_end) == (JOIN, BOUNDARY)
        if with_line:
            assert payment.period_end == BOUNDARY

    def test_anchors_use_the_full_price_not_zero(self, pending_calendar_sub: MembershipSubscription) -> None:
        subscription_stripe_sync.record_stripe_payment_from_invoice(_free_stub_invoice(), succeeded=True)

        assert subscription_notifications.last_paid_amounts([pending_calendar_sub]) == {
            pending_calendar_sub.pk: Decimal("120.00")
        }

    def test_existing_member_is_told_zero_charged_and_the_next_full_charge(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        OrganizationMember.objects.create(
            user=pending_calendar_sub.user,
            organization=pending_calendar_sub.organization,
            tier=pending_calendar_sub.plan.tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        subscription_stripe_sync.record_stripe_payment_from_invoice(_free_stub_invoice(), succeeded=True)

        context = Notification.objects.get(
            user=pending_calendar_sub.user, notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED
        ).context
        assert context["amount"].startswith("0")
        assert "120" not in context["amount"]
        assert "120" in context["next_charge_amount"]
        assert context["next_charge_date"] == "2027-01-01"


class TestStampFromTheInvoice:
    def test_stamp_is_the_lines_price_not_the_mutable_plan_price(
        self,
        pending_calendar_sub: MembershipSubscription,
        calendar_year_plan: MembershipSubscriptionPlan,
        stripe_price_retrieve: mock.MagicMock,
    ) -> None:
        """The plan was repriced (new Stripe Price) between session creation and invoice.paid."""
        calendar_year_plan.price = Decimal("150.00")
        calendar_year_plan.stripe_price_id = "price_new"
        calendar_year_plan.save(update_fields=["price", "stripe_price_id"])

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        assert payment is not None
        assert payment.full_period_amount == Decimal("120.00")
        assert stripe_price_retrieve.call_args.args[0] == "price_cal"

    def test_redelivery_never_overwrites_the_stamp(
        self,
        pending_calendar_sub: MembershipSubscription,
        calendar_year_plan: MembershipSubscriptionPlan,
        stripe_price_retrieve: mock.MagicMock,
    ) -> None:
        subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)
        calendar_year_plan.price = Decimal("150.00")
        calendar_year_plan.save(update_fields=["price"])
        stripe_price_retrieve.reset_mock()

        subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        assert MembershipPayment.objects.get(subscription=pending_calendar_sub).full_period_amount == Decimal("120.00")
        stripe_price_retrieve.assert_not_called()

    def test_subscription_update_proration_is_not_a_first_stub(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        """A mid-cycle upgrade invoice also carries a proration line but is not the join."""
        invoice = _first_invoice(invoice_id="in_upgrade")
        invoice["billing_reason"] = "subscription_update"

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount is None

    def test_full_period_joined_on_a_boundary_is_not_a_stub(self, pending_calendar_sub: MembershipSubscription) -> None:
        """A ``subscription_create`` invoice with a recurring (non-proration) line is a full period."""
        invoice = _renewal_invoice()
        invoice["billing_reason"] = "subscription_create"
        invoice["id"] = "in_boundary_join"

        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.full_period_amount is None


class TestNoticeTemplates:
    @pytest.mark.parametrize("kind", ["subscription_revival_checkout", "subscription_renewal_succeeded"])
    @pytest.mark.parametrize("ext", ["email/{}.txt", "email/{}.html", "in_app/{}.md", "telegram/{}.md"])
    def test_next_charge_is_rendered_only_when_present(self, kind: str, ext: str) -> None:
        base = {
            "plan_name": "P",
            "organization_name": "O",
            "amount": "0.00 EUR",
            "period_end": "2027-01-01",
            "checkout_url": "https://x.example/c",
        }
        name = f"notifications/{ext.format(kind)}"
        with translation.override("en"):
            plain = render_to_string(name, {"user": mock.Mock(display_name="U"), "context": base})
            noticed = render_to_string(
                name,
                {
                    "user": mock.Mock(display_name="U"),
                    "context": {**base, "next_charge_amount": "120.00 EUR", "next_charge_date": "2027-01-01"},
                },
            )

        assert "Next charge" not in plain
        assert "Next charge: 120.00 EUR on 2027-01-01." in noticed
