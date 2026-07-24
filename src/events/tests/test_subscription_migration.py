"""Tests for plan price/currency change semantics and force-migrate."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service, subscription_stripe_service


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def offline_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="mig_user", email="mig_user@example.com", password="pass")


@pytest.mark.django_db
class TestPlanCurrencyChange:
    def test_currency_change_refused_when_active_subs_exist(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as exc_info:
            subscription_service.update_plan(offline_plan, currency="USD")
        assert exc_info.value.status_code == 400
        offline_plan.refresh_from_db()
        # Plan currency unchanged
        assert offline_plan.currency == "EUR"

    def test_currency_change_allowed_when_no_active_subs(
        self,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        subscription_service.update_plan(offline_plan, currency="USD")
        offline_plan.refresh_from_db()
        assert offline_plan.currency == "USD"

    def test_currency_change_allowed_when_only_terminal_subs_exist(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Cancelled / Expired subs don't block the currency change."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(days=1),
        )
        subscription_service.update_plan(offline_plan, currency="USD")
        offline_plan.refresh_from_db()
        assert offline_plan.currency == "USD"

    def test_same_currency_value_is_noop(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Passing currency='EUR' when plan is already EUR doesn't trigger the guard."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Should NOT raise — currency value is unchanged
        subscription_service.update_plan(offline_plan, currency="EUR")
        offline_plan.refresh_from_db()
        assert offline_plan.currency == "EUR"

    def test_case_insensitive_currency_comparison(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """'eur' (lowercase) should equal 'EUR' for the guard."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Should NOT raise — case-insensitive comparison
        subscription_service.update_plan(offline_plan, currency="eur")


@pytest.mark.django_db
class TestPlanPriceChange:
    def test_price_change_with_active_subs_succeeds(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Price changes are NOT blocked — grandfathering happens at the
        Stripe Price level (ONLINE) and is a no-op for OFFLINE."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        subscription_service.update_plan(offline_plan, price=Decimal("15.00"))
        offline_plan.refresh_from_db()
        assert offline_plan.price == Decimal("15.00")


# ---- Fixtures for UpdateSubscriptionPrice tests -----------------------------


@pytest.fixture
def online_plan_g2(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """An ONLINE plan with pre-populated Stripe IDs for G2 tests."""
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="OnlineMonthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_x",
        stripe_price_id="price_new",
    )


@pytest.mark.django_db
class TestUpdateSubscriptionPrice:
    def test_swaps_price_when_different(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])

        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
        )

        with (
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": "price_old"}}]},
            }
            mock_modify.return_value = {"id": "sub_test_xyz", "status": "active"}
            result = subscription_stripe_service.update_subscription_price(sub)

        assert result is True
        mock_modify.assert_called_once()
        call_kwargs = mock_modify.call_args.kwargs
        assert call_kwargs["items"] == [{"id": "si_test", "price": "price_new"}]
        assert call_kwargs["proration_behavior"] == "none"

    def test_noop_when_already_on_current_price(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])

        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
        )

        with (
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": online_plan_g2.stripe_price_id}}]},
            }
            result = subscription_stripe_service.update_subscription_price(sub)

        assert result is False
        mock_modify.assert_not_called()

    def test_skipped_when_no_stripe_subscription_id(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id=None,
        )
        result = subscription_stripe_service.update_subscription_price(sub)
        assert result is False


@pytest.mark.django_db
class TestMigratePlanSubscribers:
    def test_migrates_offline_subscriber_and_dispatches_notification(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        from notifications.enums import NotificationType
        from notifications.models import Notification

        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Anchor the prior price via a SUCCEEDED payment, then bump the plan
        # price so the migration has a real X → Y delta to announce.
        old_price = offline_plan.price
        MembershipPayment.objects.create(
            subscription=sub,
            amount=old_price,
            currency=offline_plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now() + timedelta(days=1),
        )
        offline_plan.price = old_price + Decimal("5")
        offline_plan.save(update_fields=["price"])
        result = subscription_service.migrate_plan_subscribers(offline_plan, initiated_by=organization_owner_user)
        assert result["migrated"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert (
            Notification.objects.filter(
                user=subscriber,
                notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
            ).count()
            == 1
        )

    def test_skips_price_migration_notification_when_no_prior_payment(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """A subscription with no SUCCEEDED payments has no anchor old price, so
        the migration runs but no X→X notification is sent."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        result = subscription_service.migrate_plan_subscribers(offline_plan, initiated_by=organization_owner_user)
        assert result["migrated"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert not Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        ).exists()

    def test_skips_already_on_current_price_online_sub(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        from events.models import CustomerProfile
        from notifications.enums import NotificationType
        from notifications.models import Notification

        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])

        CustomerProfile.objects.create(user=subscriber, organization=organization, stripe_customer_id="cus_x")
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
        )
        # Stripe says sub is already on the plan's stripe_price_id → no-op
        with patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve") as mock_ret:
            mock_ret.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": online_plan_g2.stripe_price_id}}]},
            }
            result = subscription_service.migrate_plan_subscribers(online_plan_g2, initiated_by=organization_owner_user)
        assert result["skipped"] == 1
        assert result["migrated"] == 0
        # No notification fired for skipped subs
        assert not Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        ).exists()

    def test_terminal_subs_are_excluded_from_query(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        from notifications.models import Notification

        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(days=1),
        )
        result = subscription_service.migrate_plan_subscribers(offline_plan, initiated_by=organization_owner_user)
        assert result["migrated"] == 0
        assert result["skipped"] == 0
        assert Notification.objects.count() == 0

    def test_per_sub_failure_does_not_abort_others(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        organization_owner_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """If one sub's Stripe call fails, others still process."""
        from events.models import CustomerProfile

        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])

        # Create two ONLINE subs with distinct users
        for username in ["sub_a", "sub_b"]:
            user = django_user_model.objects.create_user(username=username, email=f"{username}@ex.com", password="pass")
            CustomerProfile.objects.create(user=user, organization=organization, stripe_customer_id=f"cus_{username}")
            MembershipSubscription.objects.create(
                user=user,
                plan=online_plan_g2,
                organization=organization,
                status=MembershipSubscription.SubscriptionStatus.ACTIVE,
                stripe_subscription_id=f"sub_test_{username}",
            )

        call_count: dict[str, int] = {"n": 0}

        def fake_retrieve(stripe_id: str, **kw: t.Any) -> dict[str, t.Any]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("simulated Stripe failure")
            return {
                "id": stripe_id,
                "items": {"data": [{"id": "si_test", "price": {"id": "price_old"}}]},
            }

        with (
            patch(
                "events.service.subscription_stripe_service.stripe.Subscription.retrieve",
                side_effect=fake_retrieve,
            ),
            patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            mock_modify.return_value = {"id": "sub_test_x", "status": "active"}
            result = subscription_service.migrate_plan_subscribers(online_plan_g2, initiated_by=organization_owner_user)

        assert result["failed"] == 1
        assert result["migrated"] == 1
        assert len(result["errors"]) == 1

    def test_old_price_resolved_from_last_succeeded_payment(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """_resolve_subscriber_old_price uses the last SUCCEEDED payment amount."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        # Record a historical payment at a different amount
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("8.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now(),
        )
        result = subscription_service.migrate_plan_subscribers(offline_plan, initiated_by=organization_owner_user)
        assert result["migrated"] == 1
        # Notification should have been dispatched with old_amount reflecting 8.00
        notif = Notification.objects.get(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        )
        assert "8.00" in notif.context["old_amount"]
