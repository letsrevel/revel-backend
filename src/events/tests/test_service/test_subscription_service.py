"""Tests for the subscription service layer."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from freezegun import freeze_time
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service
from events.service.subscription_service import InitialPayment

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """Use the default tier auto-created on organization save."""
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A monthly EUR plan."""
    return subscription_service.create_plan(
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


class TestPlanCrud:
    def test_create_plan(self, tier: MembershipTier) -> None:
        plan = subscription_service.create_plan(
            tier,
            name="Annual",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit="year",
            period_count=1,
        )
        assert plan.pk
        assert plan.tier_id == tier.pk
        assert plan.is_active is True

    def test_update_plan(self, plan: MembershipSubscriptionPlan) -> None:
        updated = subscription_service.update_plan(plan, price=Decimal("12.00"), description="bumped")
        updated.refresh_from_db()
        assert updated.price == Decimal("12.00")
        assert updated.description == "bumped"

    def test_update_plan_noop_returns_instance(self, plan: MembershipSubscriptionPlan) -> None:
        result = subscription_service.update_plan(plan)
        assert result.pk == plan.pk

    def test_archive_plan(self, plan: MembershipSubscriptionPlan) -> None:
        archived = subscription_service.archive_plan(plan)
        archived.refresh_from_db()
        assert archived.is_active is False

    def test_archive_plan_idempotent(self, plan: MembershipSubscriptionPlan) -> None:
        plan.is_active = False
        plan.save()
        again = subscription_service.archive_plan(plan)
        assert again.is_active is False

    def test_delete_plan(self, plan: MembershipSubscriptionPlan) -> None:
        subscription_service.delete_plan(plan)
        assert not MembershipSubscriptionPlan.objects.filter(pk=plan.pk).exists()

    def test_delete_plan_blocks_when_subscriptions_exist(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        subscription_service.create_subscription(plan, subscriber)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.delete_plan(plan)
        assert excinfo.value.status_code == 400


# ---- create_subscription -----------------------------------------------------


class TestCreateSubscription:
    def test_creates_subscription_and_member(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, tier: MembershipTier
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        # No initial payment yet: subscription stays PENDING until first record_payment.
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING
        # The signal still syncs the member to ACTIVE (PENDING maps to ACTIVE).
        member = OrganizationMember.objects.get(organization=plan.tier.organization, user=subscriber)
        assert member.tier_id == tier.pk
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_refuses_when_user_banned(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        with pytest.raises(HttpError) as excinfo:
            subscription_service.create_subscription(plan, subscriber)
        assert excinfo.value.status_code == 403

    def test_refuses_duplicate_active_subscription(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        subscription_service.create_subscription(plan, subscriber)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.create_subscription(plan, subscriber)
        assert excinfo.value.status_code == 400

    def test_allows_resubscribe_after_cancellation(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(sub, immediate=True)

        new_sub = subscription_service.create_subscription(plan, subscriber)
        assert new_sub.pk != sub.pk
        # No initial payment: a fresh subscription is PENDING.
        assert new_sub.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_initial_payment_advances_period(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        recorder: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(
                amount=Decimal("10.00"),
                currency="EUR",
                recorded_by=recorder,
                notes="paid in cash",
            ),
        )
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_start is not None
        assert sub.current_period_end is not None
        assert sub.current_period_end > sub.current_period_start
        assert sub.payments.count() == 1


# ---- record_payment ----------------------------------------------------------


class TestRecordPayment:
    def test_advances_period_on_pending(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        # The signal sync flips OrganizationMember status, but subscription
        # itself stays PENDING until the first payment.
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

        subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_end is not None

    def test_revives_past_due_to_active(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        sub.current_period_end = timezone.now() - datetime.timedelta(days=2)
        sub.save()

        subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_expired_is_terminal_and_unrevived(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        sub.save()

        subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED

    def test_renewal_anchors_to_current_period_end(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-06-01 12:00:00"):
            sub = subscription_service.create_subscription(
                plan,
                subscriber,
                initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
            )
            sub.refresh_from_db()
            first_end = sub.current_period_end
            assert first_end is not None

        # Pay again before the first period ends — renewal must extend from first_end, not now.
        with freeze_time("2026-06-15 12:00:00"):
            subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
            sub.refresh_from_db()
            assert sub.current_period_start == first_end

    def test_failed_payment_does_not_advance_period(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        before_end = sub.current_period_end

        subscription_service.record_payment(
            sub,
            amount=Decimal("10.00"),
            currency="EUR",
            recorded_by=recorder,
            status=MembershipPayment.PaymentStatus.FAILED,
        )
        sub.refresh_from_db()
        assert sub.current_period_end == before_end
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING


# ---- cancel / pause / resume -------------------------------------------------


class TestLifecycle:
    def test_cancel_at_period_end(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        out = subscription_service.cancel_subscription(sub, immediate=False)
        assert out.cancel_at_period_end is True
        assert out.status != MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_cancel_immediate(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        out = subscription_service.cancel_subscription(sub, immediate=True)
        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert out.cancelled_at is not None

    def test_cancel_terminal_is_idempotent(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(sub, immediate=True)
        again = subscription_service.cancel_subscription(sub, immediate=True)
        assert again.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_pause_and_resume(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        paused = subscription_service.pause_subscription(sub)
        assert paused.status == MembershipSubscription.SubscriptionStatus.PAUSED

        resumed = subscription_service.resume_subscription(paused)
        assert resumed.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_pause_idempotent(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        again = subscription_service.pause_subscription(sub)
        assert again.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_pause_terminal_blocked(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(sub, immediate=True)
        with pytest.raises(HttpError):
            subscription_service.pause_subscription(sub)

    def test_resume_blocked_when_not_paused(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        with pytest.raises(HttpError):
            subscription_service.resume_subscription(sub)


# ---- refund_payment ----------------------------------------------------------


class TestRefundPayment:
    def test_refund_marks_payment_only(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        payment = sub.payments.first()
        assert payment is not None
        status_before = sub.status
        period_before = sub.current_period_end

        refunded = subscription_service.refund_payment(payment, recorded_by=recorder, notes="customer asked")
        assert refunded.status == MembershipPayment.PaymentStatus.REFUNDED

        sub.refresh_from_db()
        assert sub.status == status_before
        assert sub.current_period_end == period_before

    def test_refund_idempotent(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        payment = sub.payments.first()
        assert payment is not None
        subscription_service.refund_payment(payment, recorded_by=recorder)
        again = subscription_service.refund_payment(payment, recorded_by=recorder)
        assert again.status == MembershipPayment.PaymentStatus.REFUNDED
