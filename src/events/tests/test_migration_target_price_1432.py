"""``migrate_plan_subscribers`` fetches each target Price at most once per run (#1432).

Also covers the shared ``recurring_cadence`` helper that ``ensure_stripe_price``
and the migration guard both use.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import plans as subscription_plans
from events.service.subscription.stripe.base import ensure_stripe_price, recurring_cadence

MONTHLY: dict[str, t.Any] = {"interval": "month", "interval_count": 1}
CHECKOUT = "events.service.subscription.stripe.checkout.stripe"


@pytest.fixture
def online_plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
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


def _add_online_subs(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    user_model: t.Type[RevelUser],
    names: list[str],
) -> None:
    for name in names:
        user = user_model.objects.create_user(username=name, email=f"{name}@ex.com", password="pass")
        CustomerProfile.objects.create(user=user, organization=organization, stripe_customer_id=f"cus_{name}")
        MembershipSubscription.objects.create(
            user=user,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id=f"sub_{name}",
        )


def _subscription_retrieve(stripe_id: str, **_: t.Any) -> dict[str, t.Any]:
    """A subscriber still on the old Price (monthly), so every sub needs a swap."""
    return {
        "id": stripe_id,
        "items": {"data": [{"id": "si_test", "price": {"id": "price_old", "recurring": MONTHLY}}]},
    }


@pytest.mark.django_db
class TestTargetPriceFetchedOncePerPrice:
    def test_three_subscribers_share_one_price_retrieve(
        self,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
        django_user_model: t.Type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])
        _add_online_subs(online_plan, organization, django_user_model, ["sub_a", "sub_b", "sub_c"])

        with (
            patch(f"{CHECKOUT}.Price.retrieve", return_value={"id": "price_new", "recurring": MONTHLY}) as mock_price,
            patch(f"{CHECKOUT}.Subscription.retrieve", side_effect=_subscription_retrieve),
            patch(f"{CHECKOUT}.Subscription.modify") as mock_modify,
        ):
            result = subscription_plans.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

        assert result["migrated"] == 3
        assert result["failed"] == 0
        assert mock_modify.call_count == 3
        mock_price.assert_called_once()
        assert mock_price.call_args.args[0] == "price_new"

    def test_transient_price_error_fails_one_sub_and_next_one_retries(
        self,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
        django_user_model: t.Type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        """functools.cache does not store exceptions: the failed fetch is retried by the next subscriber."""
        organization.stripe_account_id = "acct_test_xyz"
        organization.save(update_fields=["stripe_account_id"])
        _add_online_subs(online_plan, organization, django_user_model, ["sub_a", "sub_b"])

        with (
            patch(
                f"{CHECKOUT}.Price.retrieve",
                side_effect=[
                    stripe.error.APIConnectionError("simulated Stripe outage"),
                    {"id": "price_new", "recurring": MONTHLY},
                ],
            ) as mock_price,
            patch(f"{CHECKOUT}.Subscription.retrieve", side_effect=_subscription_retrieve),
            patch(f"{CHECKOUT}.Subscription.modify") as mock_modify,
        ):
            result = subscription_plans.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

        assert result["failed"] == 1
        assert result["migrated"] == 1
        assert len(result["errors"]) == 1
        assert mock_price.call_count == 2
        mock_modify.assert_called_once()


@pytest.mark.django_db
def test_cadence_is_checked_against_the_price_actually_swapped_in(
    online_plan: MembershipSubscriptionPlan,
    organization: Organization,
    django_user_model: t.Type[RevelUser],
    organization_owner_user: RevelUser,
) -> None:
    """A Price re-pointed mid-run at the same amount is cadence-checked on its own id.

    The amount is unchanged, so the run does not leave the subscriber for a re-run; the per-Price
    lookup must then fetch the new Price and refuse it because it recurs yearly.
    """
    organization.stripe_account_id = "acct_test_xyz"
    organization.save(update_fields=["stripe_account_id"])
    _add_online_subs(online_plan, organization, django_user_model, ["sub_a", "sub_b"])
    prices = {"price_new": MONTHLY, "price_yearly": {"interval": "year", "interval_count": 1}}

    def subscription_retrieve(stripe_id: str, **kwargs: t.Any) -> dict[str, t.Any]:
        # During the first subscriber's swap, the plan is re-pointed at a yearly Price, so
        # whichever subscriber comes second is migrated against it.
        MembershipSubscriptionPlan.objects.filter(pk=online_plan.pk).update(stripe_price_id="price_yearly")
        return _subscription_retrieve(stripe_id, **kwargs)

    with (
        patch(
            f"{CHECKOUT}.Price.retrieve",
            side_effect=lambda price_id, **_: {"id": price_id, "recurring": prices[price_id]},
        ) as mock_price,
        patch(f"{CHECKOUT}.Subscription.retrieve", side_effect=subscription_retrieve),
        patch(f"{CHECKOUT}.Subscription.modify") as mock_modify,
    ):
        result = subscription_plans.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

    swapped_to = [c.kwargs["items"][0]["price"] for c in mock_modify.call_args_list]
    assert swapped_to == ["price_new"], "a monthly subscriber was swapped onto a yearly Price"
    assert (result["migrated"], result["skipped"], result["failed"]) == (1, 0, 1)
    assert sorted(c.args[0] for c in mock_price.call_args_list) == ["price_new", "price_yearly"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "recurring",
    [{"interval": "year", "interval_count": 1}, {"interval": "month", "interval_count": 3}, None],
    ids=["other-interval", "other-interval-count", "not-recurring"],
)
def test_ensure_stripe_price_recreates_when_only_cadence_differs(
    online_plan: MembershipSubscriptionPlan, organization: Organization, recurring: dict[str, t.Any] | None
) -> None:
    """Amount and currency match, so only the shared cadence comparison can trigger re-provisioning."""
    organization.stripe_account_id = "acct_test_xyz"
    organization.save(update_fields=["stripe_account_id"])
    with (
        patch(
            f"{CHECKOUT}.Price.retrieve",
            return_value=stripe.Price.construct_from(
                {"id": "price_new", "active": True, "unit_amount": 1000, "currency": "eur", "recurring": recurring},
                None,
            ),
        ),
        patch(
            f"{CHECKOUT}.Price.create", return_value=stripe.Price.construct_from({"id": "price_v2"}, None)
        ) as mock_create,
        patch(f"{CHECKOUT}.Price.modify") as mock_archive,
        patch.object(organization.__class__, "is_stripe_connected", new=True, create=False),
    ):
        result = ensure_stripe_price(online_plan)
    mock_archive.assert_called_once_with("price_new", active=False, stripe_account="acct_test_xyz")
    mock_create.assert_called_once()
    assert result.stripe_price_id == "price_v2"


@pytest.mark.django_db
def test_cadence_is_read_on_the_locked_subscriptions_stripe_account(
    online_plan: MembershipSubscriptionPlan,
    organization: Organization,
    django_user_model: t.Type[RevelUser],
    organization_owner_user: RevelUser,
) -> None:
    """If the organization's Stripe account changes mid-run, later Prices are read on the new account."""
    organization.stripe_account_id = "acct_old"
    organization.save(update_fields=["stripe_account_id"])
    _add_online_subs(online_plan, organization, django_user_model, ["sub_a", "sub_b"])

    def subscription_retrieve(stripe_id: str, **kwargs: t.Any) -> dict[str, t.Any]:
        Organization.objects.filter(pk=organization.pk).update(stripe_account_id="acct_new")
        return _subscription_retrieve(stripe_id, **kwargs)

    with (
        patch(f"{CHECKOUT}.Price.retrieve", return_value={"id": "price_new", "recurring": MONTHLY}) as mock_price,
        patch(f"{CHECKOUT}.Subscription.retrieve", side_effect=subscription_retrieve),
        patch(f"{CHECKOUT}.Subscription.modify"),
    ):
        subscription_plans.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)

    accounts = [c.kwargs.get("stripe_account") for c in mock_price.call_args_list]
    assert accounts == ["acct_old", "acct_new"]


@pytest.mark.django_db
def test_ensure_stripe_price_keeps_a_price_that_omits_interval_count(
    online_plan: MembershipSubscriptionPlan, organization: Organization
) -> None:
    """Stripe may omit ``interval_count``; the shared helper reads it as 1, so a monthly Price is kept."""
    organization.stripe_account_id = "acct_test_xyz"
    organization.save(update_fields=["stripe_account_id"])
    with (
        patch(
            f"{CHECKOUT}.Price.retrieve",
            return_value=stripe.Price.construct_from(
                {
                    "id": "price_new",
                    "active": True,
                    "unit_amount": 1000,
                    "currency": "eur",
                    "recurring": {"interval": "month"},
                },
                None,
            ),
        ),
        patch(f"{CHECKOUT}.Price.create") as mock_create,
        patch(f"{CHECKOUT}.Price.modify") as mock_archive,
        patch.object(organization.__class__, "is_stripe_connected", new=True, create=False),
    ):
        result = ensure_stripe_price(online_plan)
    mock_create.assert_not_called()
    mock_archive.assert_not_called()
    assert result.stripe_price_id == "price_new"


class TestRecurringCadence:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ({"recurring": {"interval": "month", "interval_count": 1}}, ("month", 1)),
            ({"recurring": {"interval": "month", "interval_count": 3}}, ("month", 3)),
            ({"recurring": {"interval": "year"}}, ("year", 1)),
            ({"recurring": None}, None),
            ({}, None),
        ],
        ids=["monthly", "three-monthly", "missing-interval-count", "null-recurring", "non-recurring"],
    )
    def test_cadence(self, price: dict[str, t.Any], expected: tuple[str, int] | None) -> None:
        assert recurring_cadence(price) == expected
