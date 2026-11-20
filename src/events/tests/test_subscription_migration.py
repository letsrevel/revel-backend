"""Tests for plan price/currency change semantics and force-migrate."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import plans as subscription_plans
from events.service.subscription.stripe import checkout as subscription_stripe_checkout


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
            subscription_plans.update_plan(offline_plan, currency="USD")
        assert exc_info.value.status_code == 400
        offline_plan.refresh_from_db()
        # Plan currency unchanged
        assert offline_plan.currency == "EUR"

    def test_currency_change_allowed_when_no_active_subs(
        self,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        subscription_plans.update_plan(offline_plan, currency="USD")
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
        subscription_plans.update_plan(offline_plan, currency="USD")
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
        subscription_plans.update_plan(offline_plan, currency="EUR")
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
        subscription_plans.update_plan(offline_plan, currency="eur")


@pytest.mark.django_db
class TestPlanCadenceChange:
    """Regression for #1423: a cadence patch plus subscriber migration let Stripe
    stretch an ONLINE subscriber's paid period to the new interval with no invoice.
    """

    @pytest.mark.parametrize(
        "payment_method",
        [MembershipSubscriptionPlan.PaymentMethod.OFFLINE, MembershipSubscriptionPlan.PaymentMethod.ONLINE],
    )
    @pytest.mark.parametrize(
        "change",
        [{"period_unit": MembershipSubscriptionPlan.PeriodUnit.YEAR}, {"period_count": 3}],
    )
    def test_cadence_change_refused_when_active_subs_exist(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payment_method: str,
        change: dict[str, t.Any],
    ) -> None:
        offline_plan.payment_method = payment_method
        offline_plan.save(update_fields=["payment_method"])
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        )
        with (
            patch.object(subscription_plans, "ensure_stripe_price") as mock_ensure,
            pytest.raises(HttpError) as exc_info,
        ):
            subscription_plans.update_plan(offline_plan, **change)
        assert exc_info.value.status_code == 400
        assert "billing period" in str(exc_info.value)
        mock_ensure.assert_not_called()
        offline_plan.refresh_from_db()
        assert offline_plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH
        assert offline_plan.period_count == 1

    def test_cadence_change_refused_when_a_downgrade_is_scheduled_into_the_plan(
        self,
        offline_plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A pending downgrade's schedule already holds this plan's Price."""
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Yearly",
            price=Decimal("100"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=other_plan,
            pending_plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError, match="billing period"):
            subscription_plans.update_plan(offline_plan, period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR)

    def test_stale_instance_cannot_revert_cadence_of_plan_with_members(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """The guard compares against the committed row, not the caller's possibly stale instance."""
        stale = MembershipSubscriptionPlan.objects.get(pk=offline_plan.pk)  # still MONTH/1 in memory
        MembershipSubscriptionPlan.objects.filter(pk=offline_plan.pk).update(
            period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR
        )
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with CaptureQueriesContext(connection) as ctx, pytest.raises(HttpError, match="billing period"):
            subscription_plans.update_plan(stale, period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH)
        # Locks the plan row only, never the joined tier/organization rows.
        assert any('FOR UPDATE OF "events_membershipsubscriptionplan"' in q["sql"] for q in ctx.captured_queries), (
            "update_plan must re-read the plan under a row lock"
        )
        offline_plan.refresh_from_db()
        assert offline_plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.YEAR

    def test_period_count_edit_allowed_on_lifetime_plan_with_members(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """LIFETIME ignores period_count, so editing it is not a billing-period change."""
        MembershipSubscriptionPlan.objects.filter(pk=offline_plan.pk).update(
            period_unit=MembershipSubscriptionPlan.PeriodUnit.LIFETIME
        )
        offline_plan.refresh_from_db()
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        updated = subscription_plans.update_plan(offline_plan, period_count=2)
        assert updated.period_count == 2

    def test_currency_message_wins_when_currency_and_cadence_change_together(
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
            subscription_plans.update_plan(
                offline_plan, currency="USD", period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR
            )
        assert "currency" in str(exc_info.value)

    @pytest.mark.parametrize(
        "change",
        [{"period_unit": MembershipSubscriptionPlan.PeriodUnit.YEAR}, {"period_count": 3}],
    )
    def test_cadence_change_allowed_when_only_terminal_subs_exist(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        change: dict[str, t.Any],
    ) -> None:
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now() - timedelta(days=1),
        )
        updated = subscription_plans.update_plan(offline_plan, **change)
        for field, value in change.items():
            assert getattr(updated, field) == value

    def test_unchanged_cadence_values_do_not_trigger_guard(
        self,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A full-form PATCH resending the current cadence must still save other fields."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        updated = subscription_plans.update_plan(
            offline_plan,
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            period_count=1,
            name="Renamed",
        )
        assert updated.name == "Renamed"


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
        subscription_plans.update_plan(offline_plan, price=Decimal("15.00"))
        offline_plan.refresh_from_db()
        assert offline_plan.price == Decimal("15.00")


# ---- Fixtures for UpdateSubscriptionPrice tests -----------------------------


MONTHLY: dict[str, t.Any] = {"interval": "month", "interval_count": 1}


@pytest.fixture(autouse=True)
def target_price_retrieve() -> t.Iterator[mock.MagicMock]:
    """The plan's target Stripe Price recurs monthly unless a test overrides it."""
    with patch(
        "events.service.subscription.stripe.checkout.stripe.Price.retrieve",
        return_value={"id": "price_new", "recurring": MONTHLY},
    ) as mocked:
        yield mocked


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
        target_price_retrieve: mock.MagicMock,
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
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {
                    "data": [
                        {
                            "id": "si_test",
                            "price": {"id": "price_old", "recurring": {"interval": "month", "interval_count": 1}},
                        }
                    ]
                },
            }
            mock_modify.return_value = {"id": "sub_test_xyz", "status": "active"}
            result = subscription_stripe_checkout.update_subscription_price(sub)

        assert result is True
        mock_modify.assert_called_once()
        call_kwargs = mock_modify.call_args.kwargs
        assert call_kwargs["items"] == [{"id": "si_test", "price": "price_new"}]
        assert call_kwargs["proration_behavior"] == "none"
        # The target Price lives on the connected account, not the platform.
        target_price_retrieve.assert_called_once_with("price_new", stripe_account="acct_test_xyz")

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
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": online_plan_g2.stripe_price_id}}]},
            }
            result = subscription_stripe_checkout.update_subscription_price(sub)

        assert result is False
        mock_modify.assert_not_called()

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (MONTHLY, {"interval": "year", "interval_count": 1}),
            (MONTHLY, {"interval": "month", "interval_count": 3}),
            (None, MONTHLY),
            (MONTHLY, None),
        ],
    )
    def test_refuses_swap_across_billing_intervals(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        target_price_retrieve: mock.MagicMock,
        current: dict[str, t.Any] | None,
        target: dict[str, t.Any],
    ) -> None:
        """#1423: a cross-interval swap under proration 'none' stretches the paid period for free."""
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
        )
        target_price_retrieve.return_value = {"id": "price_new", "recurring": target}

        with (
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify") as mock_modify,
            pytest.raises(ValueError, match="not migrated"),
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": "price_old", "recurring": current}}]},
            }
            subscription_stripe_checkout.update_subscription_price(sub)

        mock_modify.assert_not_called()

    def test_missing_interval_count_means_one(
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
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {
                    "data": [{"id": "si_test", "price": {"id": "price_old", "recurring": {"interval": "month"}}}]
                },
            }
            assert subscription_stripe_checkout.update_subscription_price(sub) is True
        mock_modify.assert_called_once()

    def test_compares_prices_not_the_plan_row(
        self,
        online_plan_g2: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """An admin edit can leave the plan row's cadence out of sync with its Price; the Prices decide."""
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan_g2,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
        )
        MembershipSubscriptionPlan.objects.filter(pk=online_plan_g2.pk).update(
            period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR
        )
        sub.refresh_from_db()

        with (
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription.stripe.checkout.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {
                "id": "sub_test_xyz",
                "items": {"data": [{"id": "si_test", "price": {"id": "price_old", "recurring": MONTHLY}}]},
            }
            assert subscription_stripe_checkout.update_subscription_price(sub) is True

        mock_modify.assert_called_once()

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
        result = subscription_stripe_checkout.update_subscription_price(sub)
        assert result is False
