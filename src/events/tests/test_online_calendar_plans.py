"""ONLINE calendar-aligned plans: Checkout anchor, first-invoice sync, plan changes and quoted amounts (#1426).

Stripe is mocked with payloads shaped like the real ones captured in the #1424 spike
(API ``2026-03-25.dahlia``): a prorated first invoice has ``billing_reason=subscription_create``,
one line flagged ``parent.subscription_item_details.proration`` at the plan's price whose period
runs join -> boundary, and the subscription's period lives on its item.
"""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from freezegun import freeze_time
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import notifications as subscription_notifications
from events.service.subscription import refunds as subscription_refunds
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync
from events.utils.subscription_plan_rules import validate_plan_shape
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

CREATE_SESSION = "events.service.subscription.stripe.checkout.stripe.checkout.Session.create"
UTC = datetime.timezone.utc
Plan = MembershipSubscriptionPlan

JOIN = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BOUNDARY = datetime.datetime(2027, 1, 1, tzinfo=UTC)
NEXT_BOUNDARY = datetime.datetime(2028, 1, 1, tzinfo=UTC)


def _epoch(value: datetime.datetime) -> int:
    return int(value.timestamp())


@pytest.fixture(autouse=True)
def stripe_price_retrieve() -> t.Iterator[mock.MagicMock]:
    """The line's own Stripe Price (the dahlia proration line carries no ``unit_amount_decimal``)."""
    with mock.patch("stripe.Price.retrieve", return_value={"unit_amount": 12000}) as retrieve:
        yield retrieve


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


def _online_plan(tier: MembershipTier, **overrides: t.Any) -> MembershipSubscriptionPlan:
    fields: dict[str, t.Any] = {
        "tier": tier,
        "name": "Online",
        "price": Decimal("120.00"),
        "currency": "EUR",
        "period_unit": Plan.PeriodUnit.YEAR,
        "period_count": 1,
        "payment_method": Plan.PaymentMethod.ONLINE,
        "stripe_product_id": "prod_cal",
        "stripe_price_id": "price_cal",
    }
    fields.update(overrides)
    return Plan.objects.create(**fields)


@pytest.fixture
def calendar_year_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return _online_plan(tier, name="Calendar year", period_alignment=Plan.PeriodAlignment.CALENDAR)


@pytest.fixture
def rolling_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return _online_plan(tier, name="Rolling year", stripe_price_id="price_roll")


@pytest.fixture
def subscriber(django_user_model: type[RevelUser], stripe_org: Organization) -> RevelUser:
    user = django_user_model.objects.create_user(username="cal_online", email="cal_online@example.com", password="pw")
    CustomerProfile.objects.create(user=user, organization=stripe_org, stripe_customer_id="cus_cal")
    return user


def _session(session_id: str = "cs_cal") -> mock.MagicMock:
    return mock.MagicMock(id=session_id, url=f"https://checkout.stripe.com/c/pay/{session_id}")


# ---- Step 1: validation --------------------------------------------------------------------------


def _shape(**overrides: t.Any) -> str | None:
    fields: dict[str, t.Any] = {
        "payment_method": Plan.PaymentMethod.ONLINE,
        "price": Decimal("120"),
        "period_unit": Plan.PeriodUnit.YEAR,
        "period_alignment": Plan.PeriodAlignment.CALENDAR,
        "period_count": 1,
        "calendar_start_month": 9,
        "first_period_pricing": Plan.FirstPeriodPricing.PRORATED,
    }
    fields.update(overrides)
    return validate_plan_shape(**fields)


class TestShape:
    def test_online_calendar_plan_is_valid(self) -> None:
        assert _shape() is None

    def test_online_calendar_plan_can_only_prorate(self) -> None:
        message = _shape(first_period_pricing=Plan.FirstPeriodPricing.FULL)
        assert message is not None
        assert "only prorate" in message


# ---- Step 2: checkout and revival params ---------------------------------------------------------


class TestCheckoutParams:
    @freeze_time(JOIN)
    def test_calendar_plan_checkout_anchors_on_next_boundary(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(calendar_year_plan, subscriber)

        data = create.call_args.kwargs["subscription_data"]
        assert data["billing_cycle_anchor"] == _epoch(BOUNDARY)
        assert data["proration_behavior"] == "create_prorations"
        assert data["metadata"]["membership_subscription_id"]

    @freeze_time(JOIN)
    def test_rolling_plan_checkout_is_unchanged(
        self, rolling_plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(rolling_plan, subscriber)

        data = create.call_args.kwargs["subscription_data"]
        assert "billing_cycle_anchor" not in data
        assert "proration_behavior" not in data

    @freeze_time("2026-11-30 23:50:00")
    def test_session_minted_just_before_the_boundary_keeps_stripes_minimum_lifetime_plus_latency(
        self, tier: MembershipTier, subscriber: RevelUser
    ) -> None:
        """The session must not outlive its anchor; Stripe's 30 minute floor (+1 for latency) wins when closer."""
        plan = _online_plan(tier, period_unit=Plan.PeriodUnit.MONTH, period_alignment=Plan.PeriodAlignment.CALENDAR)
        with mock.patch(CREATE_SESSION, return_value=_session()) as create:
            subscription_stripe_checkout.start_online_subscription(plan, subscriber)

        now = datetime.datetime(2026, 11, 30, 23, 50, tzinfo=UTC)
        assert create.call_args.kwargs["subscription_data"]["billing_cycle_anchor"] == _epoch(
            datetime.datetime(2026, 12, 1, tzinfo=UTC)
        )
        assert create.call_args.kwargs["expires_at"] == _epoch(now + datetime.timedelta(minutes=31))

    @freeze_time("2026-11-30 10:00:00")
    def test_session_expires_before_the_anchor_when_the_default_window_would_cross_it(
        self, tier: MembershipTier, subscriber: RevelUser
    ) -> None:
        plan = _online_plan(tier, period_unit=Plan.PeriodUnit.MONTH, period_alignment=Plan.PeriodAlignment.CALENDAR)
        with (
            mock.patch(CREATE_SESSION, return_value=_session()) as create,
            mock.patch("events.utils.subscription_periods.settings.PAYMENT_DEFAULT_EXPIRY_MINUTES", 24 * 60),
        ):
            subscription_stripe_checkout.start_online_subscription(plan, subscriber)

        boundary = datetime.datetime(2026, 12, 1, tzinfo=UTC)
        assert create.call_args.kwargs["expires_at"] == _epoch(boundary - datetime.timedelta(minutes=1))

    @freeze_time(JOIN)
    def test_revival_checkout_anchors_on_next_boundary(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=calendar_year_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=JOIN - datetime.timedelta(days=1),
        )
        with mock.patch(CREATE_SESSION, return_value=_session("cs_rev")) as create:
            subscription_stripe_checkout.create_revival_checkout(sub)

        data = create.call_args.kwargs["subscription_data"]
        assert data["billing_cycle_anchor"] == _epoch(BOUNDARY)
        assert data["proration_behavior"] == "create_prorations"


# ---- Step 3: sync of the real first-invoice shape ------------------------------------------------


def _subscription_payload(*, start: datetime.datetime, end: datetime.datetime) -> dict[str, t.Any]:
    return {
        "id": "sub_cal",
        "status": "active",
        "cancel_at_period_end": False,
        "items": {
            "data": [
                {
                    "id": "si_cal",
                    "current_period_start": _epoch(start),
                    "current_period_end": _epoch(end),
                    "price": {"id": "price_cal"},
                }
            ]
        },
    }


def _first_invoice(*, amount: int = 1433, invoice_id: str = "in_first") -> dict[str, t.Any]:
    """The prorated first invoice: one proration line at the plan price, period join -> boundary."""
    return {
        "id": invoice_id,
        "billing_reason": "subscription_create",
        "amount_paid": amount,
        "amount_due": amount,
        "currency": "eur",
        "parent": {"subscription_details": {"subscription": "sub_cal", "metadata": {}}},
        "payments": {
            "data": [
                {
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": {"id": "pi_first", "application_fee_amount": 26},
                    }
                }
            ]
        },
        "lines": {
            "data": [
                {
                    "amount": amount,
                    "pricing": {"price_details": {"price": "price_cal"}},
                    "parent": {"type": "subscription_item_details", "subscription_item_details": {"proration": True}},
                    "period": {"start": _epoch(JOIN), "end": _epoch(BOUNDARY)},
                }
            ]
        },
    }


def _renewal_invoice() -> dict[str, t.Any]:
    return {
        "id": "in_renewal",
        "billing_reason": "subscription_cycle",
        "amount_paid": 12000,
        "amount_due": 12000,
        "currency": "eur",
        "parent": {"subscription_details": {"subscription": "sub_cal", "metadata": {}}},
        "payments": {
            "data": [
                {
                    "payment": {
                        "type": "payment_intent",
                        "payment_intent": {"id": "pi_renewal", "application_fee_amount": 216},
                    }
                }
            ]
        },
        "lines": {
            "data": [
                {
                    "amount": 12000,
                    "pricing": {"price_details": {"price": "price_cal"}},
                    "parent": {"type": "subscription_item_details", "subscription_item_details": {"proration": False}},
                    "period": {"start": _epoch(BOUNDARY), "end": _epoch(NEXT_BOUNDARY)},
                }
            ]
        },
    }


@pytest.fixture
def pending_calendar_sub(
    calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=calendar_year_plan,
        organization=stripe_org,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_subscription_id="sub_cal",
    )


def _join(sub: MembershipSubscription) -> MembershipPayment:
    """Replay the webhook pair for the first charge: invoice.paid then customer.subscription.updated."""
    payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)
    assert payment is not None
    subscription_stripe_sync.sync_subscription_from_stripe(_subscription_payload(start=JOIN, end=BOUNDARY))
    sub.refresh_from_db()
    return payment


class TestFirstInvoiceSync:
    def test_first_prorated_invoice_activates_the_row_on_the_boundary(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        payment = _join(pending_calendar_sub)

        assert pending_calendar_sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert pending_calendar_sub.current_period_start == JOIN
        assert pending_calendar_sub.current_period_end == BOUNDARY
        member = OrganizationMember.objects.get(
            user=pending_calendar_sub.user, organization=pending_calendar_sub.organization
        )
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert MembershipPayment.objects.filter(subscription=pending_calendar_sub).count() == 1
        assert payment.amount == Decimal("14.33")
        assert (payment.period_start, payment.period_end) == (JOIN, BOUNDARY)

    def test_first_payment_records_the_full_price_it_stood_in_for(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        payment = _join(pending_calendar_sub)

        assert payment.full_period_amount == Decimal("120.00")

    def test_redelivered_invoice_stays_one_row_with_the_full_price(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        _join(pending_calendar_sub)
        subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        payment = MembershipPayment.objects.get(subscription=pending_calendar_sub)
        assert payment.full_period_amount == Decimal("120.00")

    def test_boundary_renewal_advances_one_full_period_and_is_not_stamped(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        _join(pending_calendar_sub)

        renewal = subscription_stripe_sync.record_stripe_payment_from_invoice(_renewal_invoice(), succeeded=True)
        subscription_stripe_sync.sync_subscription_from_stripe(_subscription_payload(start=BOUNDARY, end=NEXT_BOUNDARY))

        assert renewal is not None
        assert renewal.full_period_amount is None
        assert renewal.amount == Decimal("120")
        pending_calendar_sub.refresh_from_db()
        assert pending_calendar_sub.current_period_start == BOUNDARY
        assert pending_calendar_sub.current_period_end == NEXT_BOUNDARY
        assert MembershipPayment.objects.filter(subscription=pending_calendar_sub).count() == 2

    def test_rolling_plan_first_invoice_is_not_stamped(
        self, rolling_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=rolling_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_cal",
        )
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(_first_invoice(), succeeded=True)

        assert payment is not None
        assert payment.full_period_amount is None

    def test_reminder_quotes_the_full_price_not_the_prorated_stub(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        _join(pending_calendar_sub)

        anchor = subscription_notifications.last_paid_amounts([pending_calendar_sub])
        assert anchor == {pending_calendar_sub.pk: Decimal("120.00")}

    def test_refunding_the_prorated_first_payment_cancels_the_subscription(
        self, pending_calendar_sub: MembershipSubscription
    ) -> None:
        """The ledger period_start (invoice line) must equal the subscription mirror's, or the auto-cancel no-ops."""
        payment = _join(pending_calendar_sub)
        assert payment.period_start == pending_calendar_sub.current_period_start

        subscription_refunds.refund_payment(payment, recorded_by=None)

        pending_calendar_sub.refresh_from_db()
        assert pending_calendar_sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED


# ---- Step 4: plan changes ------------------------------------------------------------------------


def _active(plan: MembershipSubscriptionPlan, user: RevelUser, org: Organization) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=org,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_active",
        current_period_start=JOIN,
        current_period_end=BOUNDARY,
    )


class TestPlanChange:
    @pytest.mark.parametrize(
        "target",
        [
            {"period_alignment": Plan.PeriodAlignment.CALENDAR, "calendar_start_month": 9},
            {"period_unit": Plan.PeriodUnit.MONTH, "period_alignment": Plan.PeriodAlignment.CALENDAR},
            {},  # rolling
        ],
        ids=["other-start-month", "other-unit", "rolling"],
    )
    def test_calendar_subscription_cannot_leave_its_grid(
        self,
        tier: MembershipTier,
        calendar_year_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
        target: dict[str, t.Any],
    ) -> None:
        sub = _active(calendar_year_plan, subscriber, stripe_org)
        other = _online_plan(tier, name="Other", price=Decimal("200"), stripe_price_id="price_other", **target)

        with (
            mock.patch("events.service.subscription.lifecycle.stripe_plan_change.change_online_plan") as change,
            mock.patch("events.service.subscription.lifecycle.ensure_plan_sales_capacity") as capacity,
            mock.patch("events.service.subscription.lifecycle.ensure_plan_on_sale") as on_sale,
        ):
            with pytest.raises(HttpError) as exc:
                subscription_lifecycle.change_plan(sub, other, enforce_eligibility=False)

        assert exc.value.status_code == 400
        assert "same billing calendar" in exc.value.message
        change.assert_not_called()
        # The shape check answers first, so a doomed switch never takes the target plan's row lock.
        capacity.assert_not_called()
        on_sale.assert_not_called()

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"calendar_start_month": 9}, "same billing calendar"),
            ({"currency": "USD"}, "same currency"),
        ],
        ids=["grid", "currency"],
    )
    def test_target_changed_while_waiting_for_the_plan_lock_is_refused(
        self,
        tier: MembershipTier,
        calendar_year_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
        change: dict[str, t.Any],
        message: str,
    ) -> None:
        """ensure_plan_sales_capacity refreshes the target after waiting out a concurrent update_plan."""
        sub = _active(calendar_year_plan, subscriber, stripe_org)
        other = _online_plan(
            tier,
            name="Other",
            price=Decimal("200"),
            stripe_price_id="price_other",
            period_alignment=Plan.PeriodAlignment.CALENDAR,
        )

        def concurrent_update_then_refresh(plan: MembershipSubscriptionPlan) -> None:
            Plan.objects.filter(pk=plan.pk).update(**change)
            plan.refresh_from_db()

        with (
            mock.patch("events.service.subscription.lifecycle.stripe_plan_change.change_online_plan") as change_online,
            mock.patch(
                "events.service.subscription.lifecycle.ensure_plan_sales_capacity",
                side_effect=concurrent_update_then_refresh,
            ),
        ):
            with pytest.raises(HttpError) as exc:
                subscription_lifecycle.change_plan(sub, other, enforce_eligibility=False)

        assert exc.value.status_code == 400
        assert message in exc.value.message
        change_online.assert_not_called()

    def test_rolling_subscription_cannot_move_onto_a_calendar_plan(
        self,
        rolling_plan: MembershipSubscriptionPlan,
        calendar_year_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
    ) -> None:
        sub = _active(rolling_plan, subscriber, stripe_org)

        with mock.patch("events.service.subscription.lifecycle.stripe_plan_change.change_online_plan") as change:
            with pytest.raises(HttpError) as exc:
                subscription_lifecycle.change_plan(sub, calendar_year_plan, enforce_eligibility=False)

        assert exc.value.status_code == 400
        change.assert_not_called()

    def test_same_grid_switch_is_allowed(
        self,
        tier: MembershipTier,
        calendar_year_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
    ) -> None:
        sub = _active(calendar_year_plan, subscriber, stripe_org)
        pricier = _online_plan(
            tier,
            name="Premium",
            price=Decimal("240"),
            stripe_price_id="price_premium",
            period_alignment=Plan.PeriodAlignment.CALENDAR,
        )

        with mock.patch(
            "events.service.subscription.lifecycle.stripe_plan_change.change_online_plan", return_value=sub
        ) as change:
            subscription_lifecycle.change_plan(sub, pricier, enforce_eligibility=False)

        change.assert_called_once()

    def test_rolling_to_rolling_switch_is_allowed(
        self,
        tier: MembershipTier,
        rolling_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        stripe_org: Organization,
    ) -> None:
        sub = _active(rolling_plan, subscriber, stripe_org)
        other = _online_plan(tier, name="Pricier", price=Decimal("240"), stripe_price_id="price_p2")

        with mock.patch(
            "events.service.subscription.lifecycle.stripe_plan_change.change_online_plan", return_value=sub
        ) as change:
            subscription_lifecycle.change_plan(sub, other, enforce_eligibility=False)

        change.assert_called_once()


# ---- Step 5: quoted amounts ----------------------------------------------------------------------


class TestQuotedAmounts:
    @freeze_time(JOIN)
    def test_revival_link_quotes_the_prorated_first_period(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=calendar_year_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_notifications._dispatch_revival_checkout(sub, checkout_url="https://checkout.example/x")

        notification = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT
        )
        quoted = notification.context["amount"]
        assert "14.3" in quoted  # 120 x (44 days minus 12h) / 365 days
        assert "120" not in quoted

    @freeze_time(JOIN)
    def test_revival_link_quotes_the_full_price_on_a_rolling_plan(
        self, rolling_plan: MembershipSubscriptionPlan, subscriber: RevelUser, stripe_org: Organization
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=rolling_plan,
            organization=stripe_org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_notifications._dispatch_revival_checkout(sub, checkout_url="https://checkout.example/x")

        notification = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT
        )
        assert "120" in notification.context["amount"]


# ---- Step 8: a session completed after the boundary it was anchored on ---------------------------


RETRIEVE_SESSION = "events.service.subscription.stripe.checkout.stripe.checkout.Session.retrieve"
EXPIRE_SESSION = "events.service.subscription.stripe.checkout.stripe.checkout.Session.expire"


def _open_session(session_id: str, created: datetime.datetime) -> mock.MagicMock:
    return mock.MagicMock(
        id=session_id,
        status="open",
        url=f"https://checkout.stripe.com/c/pay/{session_id}",
        created=_epoch(created),
    )


class TestStaleSessionPastItsAnchor:
    def test_open_session_minted_before_a_passed_boundary_is_expired_and_replaced(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        """An open session still payable past its anchor would start on a stale anchor: never resume it."""
        keys_and_anchors: list[tuple[str, int]] = []

        def _record(**kwargs: t.Any) -> mock.MagicMock:
            keys_and_anchors.append((kwargs["idempotency_key"], kwargs["subscription_data"]["billing_cycle_anchor"]))
            return _session(f"cs_{len(keys_and_anchors)}")

        minted = datetime.datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
        with mock.patch(CREATE_SESSION, side_effect=_record):
            with freeze_time(minted):
                first, _url = subscription_stripe_checkout.start_online_subscription(calendar_year_plan, subscriber)
            with (
                freeze_time("2027-01-01 00:30:00"),
                mock.patch(RETRIEVE_SESSION, return_value=_open_session("cs_1", minted)),
                mock.patch(EXPIRE_SESSION) as expire,
            ):
                second, _url = subscription_stripe_checkout.start_online_subscription(calendar_year_plan, subscriber)

        expire.assert_called_once()
        assert expire.call_args.args[0] == "cs_1"
        assert not MembershipSubscription.objects.filter(pk=first.pk).exists()
        assert second.pk != first.pk
        assert keys_and_anchors[0][0] != keys_and_anchors[1][0]
        assert keys_and_anchors[0][1] == _epoch(BOUNDARY)
        assert keys_and_anchors[1][1] == _epoch(NEXT_BOUNDARY)

    def test_open_session_within_its_period_is_still_resumed(
        self, calendar_year_plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        minted = datetime.datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
        with mock.patch(CREATE_SESSION, return_value=_session("cs_1")) as create:
            with freeze_time(minted):
                first, _url = subscription_stripe_checkout.start_online_subscription(calendar_year_plan, subscriber)
            with (
                freeze_time(JOIN),
                mock.patch(RETRIEVE_SESSION, return_value=_open_session("cs_1", minted)),
                mock.patch(EXPIRE_SESSION) as expire,
            ):
                again, url = subscription_stripe_checkout.start_online_subscription(calendar_year_plan, subscriber)

        assert again.pk == first.pk
        assert url.endswith("cs_1")
        expire.assert_not_called()
        create.assert_called_once()
