"""Schedule-managed subscribers are skipped by the force-migrate, not failed.

Regression: ``migrate_plan_subscribers`` issued ``update_subscription_price``
for every ONLINE row, including rows carrying a ``stripe_schedule_id``. Stripe
rejects a plain ``Subscription.modify`` while a schedule is attached, so those
rows 502'd into the ``failed`` bucket — and because the endpoint answers 202 and
the Celery result is only logged, staff never learned a subset had silently
stayed on the old price. The sibling ``resync_subscription_application_fees``
has always carved them out; this makes the migration agree.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def online_plan(tier: MembershipTier, organization: Organization) -> MembershipSubscriptionPlan:
    organization.stripe_account_id = "acct_test_sched"
    organization.save(update_fields=["stripe_account_id"])
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly online",
        price=Decimal("15"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_sched",
        stripe_price_id="price_new",
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="sched_user", email="sched_user@example.com", password="pass")


def _make_sub(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    user: RevelUser,
    *,
    schedule_id: str = "",
) -> MembershipSubscription:
    CustomerProfile.objects.create(user=user, organization=organization, stripe_customer_id=f"cus_{user.username}")
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id=f"sub_{user.username}",
        stripe_schedule_id=schedule_id,
    )


@pytest.mark.django_db
class TestMigrateSkipsScheduleManaged:
    def test_schedule_managed_sub_is_skipped_without_a_stripe_call(
        self,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        _make_sub(online_plan, organization, subscriber, schedule_id="sub_sched_pending_downgrade")

        with (
            patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve") as mock_retrieve,
            patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            result = subscription_service.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

        mock_retrieve.assert_not_called()
        mock_modify.assert_not_called()
        assert result["skipped_schedule_managed"] == 1
        assert result["failed"] == 0
        assert result["migrated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

    def test_unscheduled_siblings_still_migrate(
        self,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """One pending downgrade must not hold back the rest of the plan."""
        _make_sub(online_plan, organization, subscriber, schedule_id="sub_sched_pending_downgrade")
        plain = django_user_model.objects.create_user(
            username="plain_user", email="plain_user@example.com", password="pass"
        )
        _make_sub(online_plan, organization, plain)

        with (
            patch(
                "events.service.subscription_stripe_service.stripe.Subscription.retrieve",
                return_value={
                    "id": "sub_plain_user",
                    "items": {"data": [{"id": "si_plain", "price": {"id": "price_old"}}]},
                },
            ) as mock_retrieve,
            patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            result = subscription_service.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

        # Exactly one Stripe round trip: the schedule-managed row never got one.
        mock_retrieve.assert_called_once()
        mock_modify.assert_called_once()
        assert result["skipped_schedule_managed"] == 1
        assert result["migrated"] == 1
        assert result["failed"] == 0
