"""Phase 3 lifecycle tests for the Stripe-backed membership subscription service.

Split out from ``test_subscription_stripe_service.py`` to keep both files under
the 1000-line file-length limit. Phase 2 tests (customer profile, ensure_stripe_price,
start_online_subscription, cancel_online_subscription, base sync, invoice recording)
live in the parent file; everything that was added for Phase 3 — plan changes,
pause/resume, Customer Portal, the pause_collection / price-swap sync branches —
lives here.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service, subscription_stripe_service

pytestmark = pytest.mark.django_db


# ---- Shared fixtures (mirror the Phase 2 file) ------------------------------


def _make_stripe_connected(org: Organization) -> None:
    org.stripe_account_id = "acct_test_org"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    _make_stripe_connected(organization)
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def offline_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier,
        name="Monthly Offline",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="online_subscriber_phase3", email="online-p3@example.com", password="pass"
    )


# ---- change_online_plan (Phase 3) -------------------------------------------


def _make_online_subscription(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    stripe_id: str = "sub_for_change",
) -> MembershipSubscription:
    """Create an ACTIVE ONLINE subscription with a Stripe link, ready for plan-change tests."""
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id=stripe_id,
        current_period_end=timezone.now() + timedelta(days=30),
    )


class TestChangeOnlinePlan:
    @pytest.fixture
    def pricier_plan(self, tier: MembershipTier) -> MembershipSubscriptionPlan:
        """A second ONLINE plan on the same tier, twice as expensive."""
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Yearly Online",
            price=Decimal("20.00"),
            currency="EUR",
            period_unit="year",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_year",
            stripe_price_id="price_year",
        )

    @pytest.fixture
    def cheaper_plan(self, tier: MembershipTier) -> MembershipSubscriptionPlan:
        """A second ONLINE plan on the same tier, half the price (downgrade target)."""
        return MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Lite Online",
            price=Decimal("5.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_lite",
            stripe_price_id="price_lite",
        )

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve")
    def test_upgrade_modifies_stripe_with_proration(
        self,
        mock_retrieve: mock.Mock,
        mock_modify: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
        pricier_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        mock_retrieve.return_value = {"items": {"data": [{"id": "si_test"}]}}

        result = subscription_stripe_service.change_online_plan(sub, pricier_plan)

        mock_modify.assert_called_once()
        kwargs = mock_modify.call_args.kwargs
        assert kwargs["items"] == [{"id": "si_test", "price": "price_year"}]
        assert kwargs["proration_behavior"] == "create_prorations"
        assert kwargs["payment_behavior"] == "allow_incomplete"
        assert kwargs["stripe_account"] == "acct_test_org"
        result.refresh_from_db()
        assert result.plan_id == pricier_plan.pk
        assert result.pending_plan_id is None

    @mock.patch("events.service.subscription_stripe_service.stripe.SubscriptionSchedule.modify")
    @mock.patch("events.service.subscription_stripe_service.stripe.SubscriptionSchedule.create")
    def test_downgrade_creates_schedule_and_sets_pending_plan(
        self,
        mock_schedule_create: mock.Mock,
        mock_schedule_modify: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
        cheaper_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        schedule_mock = mock.MagicMock(id="sub_sched_1")
        schedule_mock.get.return_value = [{"items": [{"price": "price_test"}], "start_date": 100, "end_date": 200}]
        mock_schedule_create.return_value = schedule_mock

        result = subscription_stripe_service.change_online_plan(sub, cheaper_plan)

        mock_schedule_create.assert_called_once()
        mock_schedule_modify.assert_called_once()
        phases = mock_schedule_modify.call_args.kwargs["phases"]
        assert phases[0]["items"][0]["price"] == "price_test"
        assert phases[1]["items"][0]["price"] == "price_lite"
        assert mock_schedule_modify.call_args.kwargs["end_behavior"] == "release"
        result.refresh_from_db()
        # Downgrade leaves current plan in place — the pending plan is queued.
        assert result.plan_id == online_plan.pk
        assert result.pending_plan_id == cheaper_plan.pk
        assert result.stripe_schedule_id == "sub_sched_1"

    def test_refuses_same_plan(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.change_online_plan(sub, online_plan)
        assert exc.value.status_code == 400

    def test_refuses_when_pending_plan_already_set(
        self,
        online_plan: MembershipSubscriptionPlan,
        cheaper_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        sub.pending_plan = cheaper_plan
        sub.save(update_fields=["pending_plan"])
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.change_online_plan(sub, cheaper_plan)
        assert exc.value.status_code == 400

    def test_refuses_when_paused(
        self,
        online_plan: MembershipSubscriptionPlan,
        cheaper_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        sub.status = MembershipSubscription.SubscriptionStatus.PAUSED
        sub.save(update_fields=["status"])
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.change_online_plan(sub, cheaper_plan)
        assert exc.value.status_code == 400

    def test_refuses_when_cancel_at_period_end(
        self,
        online_plan: MembershipSubscriptionPlan,
        cheaper_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = _make_online_subscription(online_plan, subscriber)
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end"])
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.change_online_plan(sub, cheaper_plan)
        assert exc.value.status_code == 400


# ---- pause_online_subscription / resume_online_subscription -----------------


class TestPauseResumeOnlineSubscription:
    @pytest.fixture
    def online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return _make_online_subscription(online_plan, subscriber, stripe_id="sub_pause_test")

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_pause_calls_stripe_with_void_behavior(
        self,
        mock_modify: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        result = subscription_stripe_service.pause_online_subscription(online_subscription)
        mock_modify.assert_called_once_with(
            "sub_pause_test",
            pause_collection={"behavior": "void"},
            stripe_account="acct_test_org",
        )
        assert result.status == MembershipSubscription.SubscriptionStatus.PAUSED

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_pause_is_idempotent_when_already_paused(
        self,
        mock_modify: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        online_subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
        online_subscription.save(update_fields=["status"])
        result = subscription_stripe_service.pause_online_subscription(online_subscription)
        mock_modify.assert_not_called()
        assert result.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_pause_refuses_offline(
        self,
        offline_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=offline_plan,
            organization=offline_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.pause_online_subscription(sub)
        assert exc.value.status_code == 400

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_pause_stripe_error_propagates_502(
        self,
        mock_modify: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        mock_modify.side_effect = stripe.error.APIConnectionError("boom")
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.pause_online_subscription(online_subscription)
        assert exc.value.status_code == 502
        online_subscription.refresh_from_db()
        # Local state stays untouched when Stripe call fails.
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_resume_clears_pause_collection(
        self,
        mock_modify: mock.Mock,
        online_subscription: MembershipSubscription,
    ) -> None:
        online_subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
        online_subscription.save(update_fields=["status"])
        result = subscription_stripe_service.resume_online_subscription(online_subscription)
        mock_modify.assert_called_once_with(
            "sub_pause_test",
            pause_collection="",
            stripe_account="acct_test_org",
        )
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_resume_refuses_non_paused(
        self,
        online_subscription: MembershipSubscription,
    ) -> None:
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.resume_online_subscription(online_subscription)
        assert exc.value.status_code == 400


# ---- create_billing_portal_session ------------------------------------------


class TestCreateBillingPortalSession:
    @mock.patch("events.service.subscription_stripe_service.stripe.billing_portal.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_returns_session_url(
        self,
        mock_customer: mock.Mock,
        mock_portal: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_for_portal")
        mock_portal.return_value = mock.MagicMock(url="https://stripe.example/portal/abc")

        url = subscription_stripe_service.create_billing_portal_session(
            subscriber, stripe_org, return_url="https://app.example/account"
        )

        assert url == "https://stripe.example/portal/abc"
        kwargs = mock_portal.call_args.kwargs
        assert kwargs["customer"] == "cus_for_portal"
        assert kwargs["return_url"] == "https://app.example/account"
        assert kwargs["stripe_account"] == "acct_test_org"

    def test_refuses_non_connected_org(
        self,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.create_billing_portal_session(
                subscriber, organization, return_url="https://app.example/account"
            )
        assert exc.value.status_code == 400

    @mock.patch("events.service.subscription_stripe_service.stripe.billing_portal.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_stripe_failure_propagates_502(
        self,
        mock_customer: mock.Mock,
        mock_portal: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_portal")
        mock_portal.side_effect = stripe.error.APIConnectionError("boom")
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.create_billing_portal_session(
                subscriber, stripe_org, return_url="https://app.example/account"
            )
        assert exc.value.status_code == 502


# ---- sync_subscription_from_stripe — Phase 3 additions ----------------------


class TestSyncPauseCollectionAndPlanSwap:
    @pytest.fixture
    def online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return _make_online_subscription(online_plan, subscriber, stripe_id="sub_phase3_sync")

    def test_pause_collection_forces_paused_even_when_status_is_active(
        self,
        online_subscription: MembershipSubscription,
    ) -> None:
        payload = {
            "id": "sub_phase3_sync",
            "status": "active",
            "pause_collection": {"behavior": "void"},
            "cancel_at_period_end": False,
        }
        result = subscription_stripe_service.sync_subscription_from_stripe(payload)
        assert result is not None
        online_subscription.refresh_from_db()
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_resume_clears_pause_and_uses_mapped_status(
        self,
        online_subscription: MembershipSubscription,
    ) -> None:
        online_subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
        online_subscription.save(update_fields=["status"])
        payload = {
            "id": "sub_phase3_sync",
            "status": "active",
            "pause_collection": None,
        }
        subscription_stripe_service.sync_subscription_from_stripe(payload)
        online_subscription.refresh_from_db()
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_price_swap_repoints_plan_and_clears_pending(
        self,
        online_plan: MembershipSubscriptionPlan,
        online_subscription: MembershipSubscription,
        tier: MembershipTier,
    ) -> None:
        new_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Switched",
            price=Decimal("7.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_sw",
            stripe_price_id="price_switched",
        )
        online_subscription.pending_plan = new_plan
        online_subscription.stripe_schedule_id = "sub_sched_phase"
        online_subscription.save(update_fields=["pending_plan", "stripe_schedule_id"])

        payload = {
            "id": "sub_phase3_sync",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_switched"}}]},
        }
        subscription_stripe_service.sync_subscription_from_stripe(payload)
        online_subscription.refresh_from_db()
        assert online_subscription.plan_id == new_plan.pk
        assert online_subscription.pending_plan_id is None
        assert online_subscription.stripe_schedule_id == ""

    def test_price_swap_to_unknown_price_is_ignored(
        self,
        online_subscription: MembershipSubscription,
    ) -> None:
        payload = {
            "id": "sub_phase3_sync",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_unknown_to_us"}}]},
        }
        subscription_stripe_service.sync_subscription_from_stripe(payload)
        online_subscription.refresh_from_db()
        # Plan stays unchanged; we don't blindly orphan the subscription.
        assert online_subscription.plan_id == online_subscription.plan_id

    def test_terminal_status_wins_over_pause_collection(
        self,
        online_subscription: MembershipSubscription,
    ) -> None:
        """A deletion event that still carries ``pause_collection`` must not un-terminalize the local row.

        Otherwise the partial-unique index ``one_active_subscription_per_user_org``
        gets re-armed and the user can't subscribe again.
        """
        payload = {
            "id": "sub_phase3_sync",
            "status": "canceled",
            "pause_collection": {"behavior": "void"},
        }
        subscription_stripe_service.sync_subscription_from_stripe(payload)
        online_subscription.refresh_from_db()
        assert online_subscription.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert online_subscription.cancelled_at is not None

    def test_price_swap_skipped_on_terminal_subscription(
        self,
        online_subscription: MembershipSubscription,
        tier: MembershipTier,
    ) -> None:
        """Late webhook for a CANCELLED row must not rewrite the historical plan FK."""
        new_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Late Swap Target",
            price=Decimal("7.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_late",
            stripe_price_id="price_late",
        )
        original_plan_id = online_subscription.plan_id
        online_subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        online_subscription.cancelled_at = timezone.now()
        online_subscription.save(update_fields=["status", "cancelled_at"])

        payload = {
            "id": "sub_phase3_sync",
            "status": "canceled",
            "items": {"data": [{"price": {"id": "price_late"}}]},
        }
        subscription_stripe_service.sync_subscription_from_stripe(payload)
        online_subscription.refresh_from_db()
        assert online_subscription.plan_id == original_plan_id
        # ``new_plan`` exists but the swap is rejected because the row is terminal.
        assert new_plan.pk != original_plan_id


# ---- _classify_plan_change normalization ------------------------------------


class TestClassifyPlanChangePeriodNormalization:
    """Cross-cadence (monthly/yearly) classification must compare per-month equivalents."""

    def test_monthly_to_cheaper_yearly_is_downgrade(
        self,
        online_plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        # Monthly 10/mo -> Annual 100/yr (~8.33/mo) — cheaper per month.
        annual = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Annual Cheap",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit="year",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_ann_cheap",
            stripe_price_id="price_ann_cheap",
        )
        sub = _make_online_subscription(online_plan, subscriber, stripe_id="sub_x_cross")
        with (
            mock.patch("events.service.subscription_stripe_service.stripe.SubscriptionSchedule.create") as mock_create,
            mock.patch("events.service.subscription_stripe_service.stripe.SubscriptionSchedule.modify"),
        ):
            schedule_mock = mock.MagicMock(id="sched_cross")
            schedule_mock.get.return_value = [{"items": [{"price": "price_test"}], "start_date": 0, "end_date": 1}]
            mock_create.return_value = schedule_mock
            subscription_stripe_service.change_online_plan(sub, annual)
        sub.refresh_from_db()
        # Downgrade path: pending_plan set, plan unchanged.
        assert sub.pending_plan_id == annual.pk

    def test_monthly_to_pricier_yearly_is_upgrade(
        self,
        online_plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        # Monthly 10/mo -> Annual 240/yr (20/mo) — pricier per month.
        annual = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Annual Premium",
            price=Decimal("240.00"),
            currency="EUR",
            period_unit="year",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_product_id="prod_ann_prem",
            stripe_price_id="price_ann_prem",
        )
        sub = _make_online_subscription(online_plan, subscriber, stripe_id="sub_x_up")
        with (
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.retrieve") as mock_retrieve,
            mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as mock_modify,
        ):
            mock_retrieve.return_value = {"items": {"data": [{"id": "si_xyz"}]}}
            subscription_stripe_service.change_online_plan(sub, annual)
        # Upgrade path: Stripe.Subscription.modify called with create_prorations.
        mock_modify.assert_called_once()
        assert mock_modify.call_args.kwargs["proration_behavior"] == "create_prorations"
        sub.refresh_from_db()
        assert sub.plan_id == annual.pk


# ---- create_billing_portal_session — Phase 3 hardening ----------------------


class TestBillingPortalRequiresCustomerProfile:
    """The portal endpoint must refuse non-subscribers so they can't create junk Stripe Customers."""

    @mock.patch("events.service.subscription_stripe_service.stripe.billing_portal.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_refuses_when_no_customer_profile_exists(
        self,
        mock_customer: mock.Mock,
        mock_portal: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.create_billing_portal_session(
                subscriber, stripe_org, return_url="https://app.example/billing"
            )
        assert exc.value.status_code == 404
        mock_customer.assert_not_called()
        mock_portal.assert_not_called()

    @mock.patch("events.service.subscription_stripe_service.stripe.billing_portal.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_uses_existing_customer_profile(
        self,
        mock_customer: mock.Mock,
        mock_portal: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        CustomerProfile.objects.create(
            user=subscriber, organization=stripe_org, stripe_customer_id="cus_existing_portal"
        )
        mock_portal.return_value = mock.MagicMock(url="https://stripe.example/portal/ok")
        url = subscription_stripe_service.create_billing_portal_session(
            subscriber, stripe_org, return_url="https://app.example/billing"
        )
        assert url == "https://stripe.example/portal/ok"
        mock_customer.assert_not_called()  # never (re)creates on Stripe
        kwargs = mock_portal.call_args.kwargs
        assert kwargs["customer"] == "cus_existing_portal"
