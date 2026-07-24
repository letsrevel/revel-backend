"""Tests for plan sale controls: max_subscriptions cap + sales_status pause.

The cap counts non-terminal subscriptions (the venue's "card stock"), so a
cancelled/expired subscription frees its slot automatically. PAUSED sales
block member self-service paths only; staff endpoints bypass the pause but
never the cap.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.schema.subscription import PublicPlanSchema
from events.service import subscription_service, subscription_stripe_service
from events.service.subscription_service import InitialPayment

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Card holders")


@pytest.fixture
def capped_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly capped",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        max_subscriptions=2,
    )


@pytest.fixture
def staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="cap_staff", email="cap_staff@example.com", password="pass")


def _user(i: int, django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username=f"cap_user_{i}", email=f"cap_user_{i}@example.com", password="pass"
    )


class TestSubscriptionCap:
    def test_cap_blocks_creation_when_full(
        self,
        capped_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        subscription_service.create_subscription(capped_plan, _user(1, django_user_model))
        subscription_service.create_subscription(capped_plan, _user(2, django_user_model))

        with pytest.raises(HttpError) as exc:
            subscription_service.create_subscription(capped_plan, _user(3, django_user_model))
        assert exc.value.status_code == 400
        assert "sold out" in str(exc.value.message).lower()

    def test_terminalized_subscription_frees_a_slot(
        self,
        capped_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        first = subscription_service.create_subscription(capped_plan, _user(1, django_user_model))
        subscription_service.create_subscription(capped_plan, _user(2, django_user_model))

        # Slot reclaim: cancelling makes room for a new subscriber.
        subscription_service.cancel_subscription(first, immediate=True)

        sub = subscription_service.create_subscription(capped_plan, _user(3, django_user_model))
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_uncapped_plan_is_unlimited(
        self,
        tier: MembershipTier,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly uncapped",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        for i in range(1, 4):
            subscription_service.create_subscription(plan, _user(i, django_user_model))
        assert plan.subscriptions.count() == 3

    def test_cap_blocks_revival_when_full(
        self,
        capped_plan: MembershipSubscriptionPlan,
        staff_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """A revived sub re-occupies a slot, so a full plan refuses revival — even for staff."""
        expired_user = _user(1, django_user_model)
        expired = MembershipSubscription.objects.create(
            user=expired_user,
            plan=capped_plan,
            organization=capped_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )
        subscription_service.create_subscription(capped_plan, _user(2, django_user_model))
        subscription_service.create_subscription(capped_plan, _user(3, django_user_model))

        payment = InitialPayment(amount=capped_plan.price, currency=capped_plan.currency, recorded_by=staff_user)
        with pytest.raises(HttpError) as exc:
            subscription_service.revive_subscription(expired, initial_payment=payment, enforce_sales_status=False)
        assert exc.value.status_code == 400
        expired.refresh_from_db()
        assert expired.status == MembershipSubscription.SubscriptionStatus.EXPIRED

    def test_cap_blocks_change_plan_into_full_plan(
        self,
        tier: MembershipTier,
        capped_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """Switching into a full plan is refused regardless of enforce_sales_status."""
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly other",
            price=Decimal("8"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        subscription_service.create_subscription(capped_plan, _user(1, django_user_model))
        subscription_service.create_subscription(capped_plan, _user(2, django_user_model))
        mover = subscription_service.create_subscription(other_plan, _user(3, django_user_model))
        mover.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        mover.save(update_fields=["status"])

        with pytest.raises(HttpError) as exc:
            subscription_service.change_plan(mover, capped_plan, enforce_sales_status=False)
        assert exc.value.status_code == 400


class TestSalesStatusPause:
    @pytest.fixture
    def paused_plan(self, tier: MembershipTier) -> MembershipSubscriptionPlan:
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly paused",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
            sales_status=MembershipSubscriptionPlan.SalesStatus.PAUSED,
        )

    def test_member_online_subscribe_refused_on_paused_plan(
        self,
        tier: MembershipTier,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        online_paused = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly online paused",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            sales_status=MembershipSubscriptionPlan.SalesStatus.PAUSED,
            stripe_product_id="prod_p",
            stripe_price_id="price_p",
        )
        with (
            pytest.raises(HttpError) as exc,
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create") as mock_create,
        ):
            subscription_stripe_service.start_online_subscription(online_paused, _user(1, django_user_model))
        assert exc.value.status_code == 400
        mock_create.assert_not_called()

    def test_staff_offline_create_bypasses_pause(
        self,
        paused_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """Pausing public sales must not stop staff from managing subscriptions manually."""
        sub = subscription_service.create_subscription(paused_plan, _user(1, django_user_model))
        assert sub.pk is not None

    def test_member_revival_refused_on_paused_plan(
        self,
        paused_plan: MembershipSubscriptionPlan,
        staff_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        expired = MembershipSubscription.objects.create(
            user=_user(1, django_user_model),
            plan=paused_plan,
            organization=paused_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )
        payment = InitialPayment(amount=paused_plan.price, currency=paused_plan.currency, recorded_by=staff_user)

        with pytest.raises(HttpError) as exc:
            subscription_service.revive_subscription(expired, initial_payment=payment)
        assert exc.value.status_code == 400

    def test_staff_revival_bypasses_pause(
        self,
        paused_plan: MembershipSubscriptionPlan,
        staff_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        expired = MembershipSubscription.objects.create(
            user=_user(1, django_user_model),
            plan=paused_plan,
            organization=paused_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )
        payment = InitialPayment(amount=paused_plan.price, currency=paused_plan.currency, recorded_by=staff_user)

        revived, secret = subscription_service.revive_subscription(
            expired, initial_payment=payment, enforce_sales_status=False
        )
        assert secret is None
        assert revived.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_member_change_plan_into_paused_plan_refused(
        self,
        tier: MembershipTier,
        paused_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        source = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly source",
            price=Decimal("8"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        sub = subscription_service.create_subscription(source, _user(1, django_user_model))
        sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        sub.save(update_fields=["status"])

        with pytest.raises(HttpError) as exc:
            subscription_service.change_plan(sub, paused_plan)
        assert exc.value.status_code == 400

        # Staff bypass the pause (but not the cap — covered separately).
        moved = subscription_service.change_plan(sub, paused_plan, enforce_sales_status=False)
        assert moved.plan_id == paused_plan.pk


class TestPublicPlanAvailability:
    def test_sold_out_resolver(
        self,
        capped_plan: MembershipSubscriptionPlan,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        assert PublicPlanSchema.resolve_sold_out(capped_plan) is False

        subscription_service.create_subscription(capped_plan, _user(1, django_user_model))
        subscription_service.create_subscription(capped_plan, _user(2, django_user_model))
        assert PublicPlanSchema.resolve_sold_out(capped_plan) is True

        # Terminal subs free their slot.
        MembershipSubscription.objects.filter(plan=capped_plan).update(
            status=MembershipSubscription.SubscriptionStatus.CANCELLED
        )
        assert PublicPlanSchema.resolve_sold_out(capped_plan) is False
