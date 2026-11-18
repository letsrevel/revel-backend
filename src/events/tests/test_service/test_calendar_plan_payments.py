"""OFFLINE payments on calendar-aligned plans: period boundaries, chaining and lapsed backfills."""

import datetime
from decimal import Decimal

import pytest
from freezegun import freeze_time
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import notifications as subscription_notifications
from events.service.subscription import payment_edits
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month", period_count=1
    )


@pytest.fixture
def calendar_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier,
        name="Calendar",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        period_alignment="calendar",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="subscriber", email="subscriber@example.com", password="pass")


@pytest.fixture
def recorder(organization_owner_user: RevelUser) -> RevelUser:
    return organization_owner_user


class TestCalendarPlanPayments:
    def test_calendar_plan_payment_ends_at_boundary_and_chains(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            subscription_lifecycle.record_payment(sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder)
            sub.refresh_from_db()
            assert sub.current_period_end == datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)

            # A second payment chains one full calendar period from the previous end.
            subscription_lifecycle.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
            sub.refresh_from_db()
            assert sub.current_period_end == datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)

    def test_calendar_plan_backfill_into_lapsed_period_is_refused(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-01 08:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
        with freeze_time("2026-05-03 12:00:00"):
            # A rolling plan would run Apr 20 -> May 20 and accept; the calendar period ended May 1: lapsed.
            with pytest.raises(HttpError) as exc_info:
                subscription_lifecycle.record_payment(
                    sub,
                    amount=Decimal("10.00"),
                    currency="EUR",
                    recorded_by=recorder,
                    occurred_at=datetime.datetime(2026, 4, 20, tzinfo=datetime.timezone.utc),
                )
            assert exc_info.value.status_code == 400

    def test_calendar_plan_backfill_into_current_period_is_accepted(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-01 08:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
        with freeze_time("2026-04-25 12:00:00"):
            subscription_lifecycle.record_payment(
                sub,
                amount=Decimal("10.00"),
                currency="EUR",
                recorded_by=recorder,
                occurred_at=datetime.datetime(2026, 4, 20, tzinfo=datetime.timezone.utc),
            )
            sub.refresh_from_db()
            assert sub.current_period_end == datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)

    def test_calendar_year_plan_with_september_start(
        self, tier: MembershipTier, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        plan = subscription_plans.create_plan(
            tier,
            name="Season",
            price=Decimal("120.00"),
            currency="EUR",
            period_unit="year",
            period_alignment="calendar",
            calendar_start_month=9,
        )
        with freeze_time("2026-03-10 12:00:00"):
            sub = subscription_lifecycle.create_subscription(plan, subscriber)
            subscription_lifecycle.record_payment(sub, amount=Decimal("60.00"), currency="EUR", recorded_by=recorder)
            sub.refresh_from_db()
            assert sub.current_period_end == datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)

            subscription_lifecycle.record_payment(sub, amount=Decimal("120.00"), currency="EUR", recorded_by=recorder)
            sub.refresh_from_db()
            assert sub.current_period_end == datetime.datetime(2027, 9, 1, tzinfo=datetime.timezone.utc)


class TestFullPeriodAmount:
    """``full_period_amount`` records the plan price a prorated first calendar period stands in for."""

    def test_prorated_first_payment_records_plan_price_and_renewal_does_not(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            first = subscription_lifecycle.record_payment(
                sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder
            )
            sub.refresh_from_db()
            renewal = subscription_lifecycle.record_payment(
                sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
            )
        assert first.full_period_amount == Decimal("10.00")
        assert renewal.full_period_amount is None

    def test_full_pricing_first_payment_records_none(
        self, tier: MembershipTier, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        plan = subscription_plans.create_plan(
            tier,
            name="Full",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_alignment="calendar",
            first_period_pricing="full",
        )
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
            )
        assert payment.full_period_amount is None

    def test_rolling_first_payment_records_none(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_lifecycle.create_subscription(plan, subscriber)
        payment = subscription_lifecycle.record_payment(
            sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
        )
        assert payment.full_period_amount is None

    def test_first_payment_edited_to_succeeded_records_plan_price(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """A first payment recorded as failed and later corrected to SUCCEEDED is still a prorated first period."""
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub,
                amount=Decimal("5.00"),
                currency="EUR",
                recorded_by=recorder,
                status=MembershipPayment.PaymentStatus.FAILED,
            )
            assert payment.full_period_amount is None
            payment_edits.update_payment(
                payment, changes={"status": MembershipPayment.PaymentStatus.SUCCEEDED}, recorded_by=recorder
            )
        payment.refresh_from_db()
        assert payment.full_period_amount == Decimal("10.00")

    def test_late_renewal_is_not_stamped(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """A renewal paid a day late starts mid-period but is paid in full: it anchors on what was paid."""
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            subscription_lifecycle.record_payment(sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder)
        sub.refresh_from_db()
        with freeze_time("2026-05-02 09:00:00"):
            late = subscription_lifecycle.record_payment(
                sub,
                amount=Decimal("8.00"),
                currency="EUR",
                recorded_by=recorder,
                occurred_at=datetime.datetime(2026, 5, 2, 9, tzinfo=datetime.timezone.utc),
            )
        assert late.full_period_amount is None

    def test_first_payment_edited_to_failed_clears_the_stamp(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder
            )
            assert payment.full_period_amount == Decimal("10.00")
            payment_edits.update_payment(
                payment, changes={"status": MembershipPayment.PaymentStatus.FAILED}, recorded_by=recorder
            )
        payment.refresh_from_db()
        assert payment.full_period_amount is None

    def test_recorded_prorated_join_anchors_reminders_at_full_price(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """End to end: record_payment stamps the join, and the shared anchor (MRR, notices, reminders) reads it."""
        with freeze_time("2026-04-28 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            subscription_lifecycle.record_payment(sub, amount=Decimal("1.00"), currency="EUR", recorded_by=recorder)
        anchors = subscription_notifications.last_paid_amounts(MembershipSubscription.objects.filter(pk=sub.pk))
        assert anchors[sub.pk] == Decimal("10.00")

    def test_date_correction_keeps_stamp_after_pricing_switched_to_full(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """Changing the plan's first-period pricing later must not erase the price captured at payment time."""
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder
            )
            MembershipSubscriptionPlan.objects.filter(pk=calendar_plan.pk).update(
                first_period_pricing=MembershipSubscriptionPlan.FirstPeriodPricing.FULL
            )
            payment_edits.update_payment(
                payment,
                changes={"occurred_at": datetime.datetime(2026, 4, 15, 9, tzinfo=datetime.timezone.utc)},
                recorded_by=recorder,
            )
        payment.refresh_from_db()
        assert payment.full_period_amount == Decimal("10.00")

    def test_boundary_exact_first_payment_records_none(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """Joining exactly on a boundary buys a full-length period, so there is no stub to anchor."""
        with freeze_time("2026-05-01 00:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
            )
        assert payment.full_period_amount is None

    def test_rolling_to_calendar_switch_stub_is_not_stamped(
        self,
        plan: MembershipSubscriptionPlan,
        calendar_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        recorder: RevelUser,
    ) -> None:
        """Documented ceiling: only a first payment is stamped, so the post-switch stub anchors on what was paid."""
        with freeze_time("2026-04-10 12:00:00"):
            sub = subscription_lifecycle.create_subscription(plan, subscriber)
            first = subscription_lifecycle.record_payment(
                sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
            )
            assert first.full_period_amount is None
            sub.refresh_from_db()
            sub = subscription_lifecycle.staff_change_plan(sub, calendar_plan)
            stub = subscription_lifecycle.record_payment(
                sub, amount=Decimal("4.00"), currency="EUR", recorded_by=recorder
            )
        assert stub.period_start == datetime.datetime(2026, 5, 10, 12, tzinfo=datetime.timezone.utc)
        assert stub.period_end == datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        assert stub.full_period_amount is None

    def test_edit_keeps_the_price_captured_at_payment_time(
        self, calendar_plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-04-16 12:00:00"):
            sub = subscription_lifecycle.create_subscription(calendar_plan, subscriber)
            payment = subscription_lifecycle.record_payment(
                sub, amount=Decimal("5.00"), currency="EUR", recorded_by=recorder
            )
            assert payment.full_period_amount == Decimal("10.00")
            calendar_plan.price = Decimal("15.00")
            calendar_plan.save(update_fields=["price"])
            payment_edits.update_payment(
                payment,
                changes={"occurred_at": datetime.datetime(2026, 4, 14, 12, tzinfo=datetime.timezone.utc)},
                recorded_by=recorder,
            )
        payment.refresh_from_db()
        assert payment.full_period_amount == Decimal("10.00")
