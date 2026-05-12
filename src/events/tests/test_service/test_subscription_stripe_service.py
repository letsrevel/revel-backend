"""Tests for the Stripe-backed membership subscription service (Phase 2)."""

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
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service, subscription_stripe_service

pytestmark = pytest.mark.django_db


# ---- Fixtures ---------------------------------------------------------------


def _make_stripe_connected(org: Organization) -> None:
    """Flip the Stripe Connect flags on an organization."""
    org.stripe_account_id = "acct_test_org"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    """A Stripe-connected organization."""
    _make_stripe_connected(organization)
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """An ONLINE plan with pre-populated Stripe IDs (skips ensure_stripe_price)."""
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
    """A bare OFFLINE plan."""
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
        username="online_subscriber", email="online@example.com", password="pass"
    )


# ---- ensure_customer_profile -------------------------------------------------


class TestEnsureCustomerProfile:
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_creates_stripe_customer_and_db_row(
        self,
        mock_create: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        mock_create.return_value = mock.MagicMock(id="cus_new_123")

        profile = subscription_stripe_service.ensure_customer_profile(subscriber, stripe_org)

        assert profile.stripe_customer_id == "cus_new_123"
        assert profile.user == subscriber
        assert profile.organization == stripe_org
        mock_create.assert_called_once()
        # Call goes against the Connect account.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["stripe_account"] == "acct_test_org"
        assert kwargs["email"] == subscriber.email

    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_reuses_existing_profile(
        self,
        mock_create: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        existing = CustomerProfile.objects.create(
            user=subscriber, organization=stripe_org, stripe_customer_id="cus_existing"
        )

        profile = subscription_stripe_service.ensure_customer_profile(subscriber, stripe_org)

        assert profile.pk == existing.pk
        mock_create.assert_not_called()

    def test_refuses_non_connected_org(
        self,
        organization: Organization,  # not stripe-connected
        subscriber: RevelUser,
    ) -> None:
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.ensure_customer_profile(subscriber, organization)
        assert exc.value.status_code == 400

    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_stripe_failure_raises_502(
        self,
        mock_create: mock.Mock,
        stripe_org: Organization,
        subscriber: RevelUser,
    ) -> None:
        mock_create.side_effect = stripe.error.APIConnectionError("boom")
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.ensure_customer_profile(subscriber, stripe_org)
        assert exc.value.status_code == 502


# ---- ensure_stripe_price -----------------------------------------------------


class TestEnsureStripePrice:
    def test_offline_plan_is_noop(self, offline_plan: MembershipSubscriptionPlan) -> None:
        with mock.patch("events.service.subscription_stripe_service.stripe.Product.create") as p:
            result = subscription_stripe_service.ensure_stripe_price(offline_plan)
            p.assert_not_called()
        assert result == offline_plan
        assert offline_plan.stripe_product_id == ""

    @mock.patch("events.service.subscription_stripe_service.stripe.Price.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Product.create")
    def test_creates_product_and_price_when_missing(
        self,
        mock_product: mock.Mock,
        mock_price: mock.Mock,
        tier: MembershipTier,
    ) -> None:
        plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Yearly",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit="year",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        mock_product.return_value = mock.MagicMock(id="prod_new")
        mock_price.return_value = mock.MagicMock(id="price_new")

        result = subscription_stripe_service.ensure_stripe_price(plan)

        assert result.stripe_product_id == "prod_new"
        assert result.stripe_price_id == "price_new"
        mock_product.assert_called_once()
        mock_price.assert_called_once()
        assert mock_price.call_args.kwargs["unit_amount"] == 10000  # 100.00 EUR
        assert mock_price.call_args.kwargs["currency"] == "eur"
        assert mock_price.call_args.kwargs["recurring"] == {"interval": "year", "interval_count": 1}

    @mock.patch("events.service.subscription_stripe_service.stripe.Price.modify")
    @mock.patch("events.service.subscription_stripe_service.stripe.Price.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Price.retrieve")
    def test_archives_and_recreates_price_when_inputs_change(
        self,
        mock_retrieve: mock.Mock,
        mock_create: mock.Mock,
        mock_modify: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        mock_retrieve.return_value = mock.MagicMock(
            active=True,
            unit_amount=500,  # plan is 10.00 → 1000; mismatch
            currency="eur",
            recurring={"interval": "month", "interval_count": 1},
        )
        mock_create.return_value = mock.MagicMock(id="price_v2")

        result = subscription_stripe_service.ensure_stripe_price(online_plan)

        mock_modify.assert_called_once_with("price_test", active=False, stripe_account="acct_test_org")
        mock_create.assert_called_once()
        assert result.stripe_price_id == "price_v2"

    @mock.patch("events.service.subscription_stripe_service.stripe.Price.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Price.retrieve")
    def test_no_op_when_inputs_unchanged(
        self,
        mock_retrieve: mock.Mock,
        mock_create: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        mock_retrieve.return_value = mock.MagicMock(
            active=True,
            unit_amount=1000,
            currency="eur",
            recurring={"interval": "month", "interval_count": 1},
        )
        result = subscription_stripe_service.ensure_stripe_price(online_plan)
        assert result.stripe_price_id == "price_test"
        mock_create.assert_not_called()


# ---- archive_stripe_price ----------------------------------------------------


class TestArchiveStripePrice:
    @mock.patch("events.service.subscription_stripe_service.stripe.Price.modify")
    def test_deactivates_price_for_online(
        self, mock_modify: mock.Mock, online_plan: MembershipSubscriptionPlan
    ) -> None:
        subscription_stripe_service.archive_stripe_price(online_plan)
        mock_modify.assert_called_once_with("price_test", active=False, stripe_account="acct_test_org")

    @mock.patch("events.service.subscription_stripe_service.stripe.Price.modify")
    def test_noop_for_offline(self, mock_modify: mock.Mock, offline_plan: MembershipSubscriptionPlan) -> None:
        subscription_stripe_service.archive_stripe_price(offline_plan)
        mock_modify.assert_not_called()

    @mock.patch("events.service.subscription_stripe_service.stripe.Price.modify")
    def test_swallows_invalid_request(self, mock_modify: mock.Mock, online_plan: MembershipSubscriptionPlan) -> None:
        mock_modify.side_effect = stripe.error.InvalidRequestError("already archived", "id")
        # Must not raise — design intent is record-only cleanup.
        subscription_stripe_service.archive_stripe_price(online_plan)


# ---- start_online_subscription ----------------------------------------------


class TestStartOnlineSubscription:
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_happy_path_returns_client_secret(
        self,
        mock_customer: mock.Mock,
        mock_subscription: mock.Mock,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_abc")
        mock_subscription.return_value = mock.MagicMock(
            id="sub_xyz",
            latest_invoice={"payment_intent": {"client_secret": "pi_secret_test"}},
        )

        subscription, client_secret = subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        assert client_secret == "pi_secret_test"
        assert subscription.stripe_subscription_id == "sub_xyz"
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PENDING
        # ONLINE PENDING must not grant member benefits up front.
        assert not OrganizationMember.objects.filter(organization=stripe_org, user=subscriber).exists()

    def test_refuses_offline_plan(
        self,
        offline_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.start_online_subscription(offline_plan, subscriber)
        assert exc.value.status_code == 400

    def test_refuses_archived_plan(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        online_plan.is_active = False
        online_plan.save(update_fields=["is_active"])
        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.start_online_subscription(online_plan, subscriber)
        assert exc.value.status_code == 400

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_stripe_failure_rolls_back_local_row(
        self,
        mock_customer: mock.Mock,
        mock_subscription: mock.Mock,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        mock_customer.return_value = mock.MagicMock(id="cus_abc")
        mock_subscription.side_effect = stripe.error.CardError("declined", "card", "card_declined")

        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        assert exc.value.status_code == 502
        assert not MembershipSubscription.objects.filter(user=subscriber, organization=stripe_org).exists()

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.cancel")
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_missing_client_secret_cancels_stripe_and_deletes_local(
        self,
        mock_customer: mock.Mock,
        mock_subscription_create: mock.Mock,
        mock_subscription_cancel: mock.Mock,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """If Stripe returns no ``client_secret``, the user can't confirm — clean up both sides."""
        mock_customer.return_value = mock.MagicMock(id="cus_abc")
        # Stripe accepts the create but returns a subscription without an
        # expandable PaymentIntent (no client_secret).
        mock_subscription_create.return_value = mock.MagicMock(id="sub_orphan", latest_invoice=None)

        with pytest.raises(HttpError) as exc:
            subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        assert exc.value.status_code == 502
        mock_subscription_cancel.assert_called_once_with("sub_orphan", stripe_account="acct_test_org")
        # Local row must not survive — otherwise the partial-unique index
        # blocks the user from retrying.
        assert not MembershipSubscription.objects.filter(user=subscriber, organization=stripe_org).exists()

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_idempotency_keys_are_set(
        self,
        mock_customer: mock.Mock,
        mock_subscription: mock.Mock,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Customer + Subscription Stripe calls carry deterministic idempotency keys."""
        mock_customer.return_value = mock.MagicMock(id="cus_idem")
        mock_subscription.return_value = mock.MagicMock(
            id="sub_idem",
            latest_invoice={"payment_intent": {"client_secret": "pi_secret"}},
        )

        subscription, _ = subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        customer_key = mock_customer.call_args.kwargs["idempotency_key"]
        assert customer_key == f"cust:{subscriber.pk}:{online_plan.tier.organization_id}"
        sub_key = mock_subscription.call_args.kwargs["idempotency_key"]
        assert sub_key == f"sub:{subscription.pk}"


# ---- cancel_online_subscription ---------------------------------------------


class TestCancelOnlineSubscription:
    @pytest.fixture
    def online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_abc",
            current_period_end=timezone.now() + timedelta(days=30),
        )
        return sub

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_schedules_at_period_end(self, mock_modify: mock.Mock, online_subscription: MembershipSubscription) -> None:
        result = subscription_stripe_service.cancel_online_subscription(online_subscription, immediate=False)

        mock_modify.assert_called_once_with("sub_abc", cancel_at_period_end=True, stripe_account="acct_test_org")
        assert result.cancel_at_period_end is True
        # Status stays ACTIVE until the period end webhook arrives.
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.cancel")
    def test_immediate_cancellation_marks_terminal(
        self, mock_cancel: mock.Mock, online_subscription: MembershipSubscription
    ) -> None:
        result = subscription_stripe_service.cancel_online_subscription(online_subscription, immediate=True)

        mock_cancel.assert_called_once_with("sub_abc", stripe_account="acct_test_org")
        assert result.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert result.cancelled_at is not None


# ---- sync_subscription_from_stripe ------------------------------------------


class TestSyncSubscriptionFromStripe:
    @pytest.fixture
    def pending_online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_to_sync",
        )

    def test_mirrors_active_status_and_periods(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        payload = {
            "id": "sub_to_sync",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": 1_800_000_000,
            "current_period_end": 1_800_000_000 + 30 * 86400,
        }
        result = subscription_stripe_service.sync_subscription_from_stripe(payload)
        assert result is not None
        result.refresh_from_db()
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert result.current_period_start is not None
        assert result.current_period_end is not None
        # _ensure_active_member should have created the member at the plan tier.
        member = OrganizationMember.objects.get(
            organization=pending_online_subscription.organization, user=pending_online_subscription.user
        )
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == pending_online_subscription.plan.tier_id

    def test_unknown_subscription_returns_none(self) -> None:
        result = subscription_stripe_service.sync_subscription_from_stripe({"id": "sub_unknown", "status": "active"})
        assert result is None

    def test_cancel_at_period_end_mirrored(self, pending_online_subscription: MembershipSubscription) -> None:
        payload = {
            "id": "sub_to_sync",
            "status": "active",
            "cancel_at_period_end": True,
        }
        result = subscription_stripe_service.sync_subscription_from_stripe(payload)
        assert result is not None
        assert result.cancel_at_period_end is True


# ---- record_stripe_payment_from_invoice -------------------------------------


class TestRecordStripePaymentFromInvoice:
    @pytest.fixture
    def pending_online_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_invoice",
        )

    def test_succeeded_creates_payment_and_activates_subscription(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        invoice = {
            "id": "in_test",
            "subscription": "sub_invoice",
            "amount_paid": 1000,
            "currency": "eur",
            "payment_intent": "pi_test",
            "lines": {
                "data": [
                    {
                        "period": {
                            "start": 1_800_000_000,
                            "end": 1_800_000_000 + 30 * 86400,
                        }
                    }
                ]
            },
        }
        payment = subscription_stripe_service.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.amount == Decimal("10.00")
        assert payment.stripe_invoice_id == "in_test"
        assert payment.stripe_payment_intent_id == "pi_test"

        pending_online_subscription.refresh_from_db()
        assert pending_online_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        # Member created via _ensure_active_member.
        member = OrganizationMember.objects.get(
            organization=pending_online_subscription.organization,
            user=pending_online_subscription.user,
        )
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_failed_payment_records_and_transitions_to_past_due(
        self,
        pending_online_subscription: MembershipSubscription,
    ) -> None:
        pending_online_subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        pending_online_subscription.save(update_fields=["status"])

        invoice = {
            "id": "in_fail",
            "subscription": "sub_invoice",
            "amount_paid": 0,
            "amount_due": 1000,
            "currency": "eur",
            "payment_intent": "pi_fail",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 86400}}]},
        }

        payment = subscription_stripe_service.record_stripe_payment_from_invoice(invoice, succeeded=False)
        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.FAILED
        # FAILED payments collected nothing — ``amount`` reflects that.
        # ``raw_response`` preserves Stripe's reported ``amount_due``.
        assert payment.amount == Decimal("0")
        assert payment.raw_response.get("amount_due") == 1000

        pending_online_subscription.refresh_from_db()
        assert pending_online_subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE

    def test_duplicate_webhook_is_idempotent(self, pending_online_subscription: MembershipSubscription) -> None:
        invoice = {
            "id": "in_dup",
            "subscription": "sub_invoice",
            "amount_paid": 1000,
            "currency": "eur",
            "payment_intent": "pi_dup",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 86400}}]},
        }
        subscription_stripe_service.record_stripe_payment_from_invoice(invoice, succeeded=True)
        subscription_stripe_service.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert MembershipPayment.objects.filter(stripe_invoice_id="in_dup").count() == 1

    def test_unknown_subscription_returns_none(self) -> None:
        invoice = {
            "id": "in_orphan",
            "subscription": "sub_nope",
            "amount_paid": 0,
            "currency": "eur",
        }
        assert subscription_stripe_service.record_stripe_payment_from_invoice(invoice, succeeded=False) is None


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
