"""Tests for the subscription service layer."""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone
from freezegun import freeze_time
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import BillingInfoRequiredError, StripeNotConnectedError
from events.models import (
    Blacklist,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_refunds, subscription_service
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


# ---- ONLINE plan billing-info gate -------------------------------------------


def _connect_stripe(organization: Organization) -> None:
    """Flip the Stripe Connect flags without touching billing info."""
    organization.stripe_account_id = "acct_test_plan_gate"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


def _set_billing_info(organization: Organization) -> None:
    """Fill in the billing fields the platform-fee invoice needs."""
    organization.billing_name = "Acme Ltd"
    organization.billing_address = "1 Acme St"
    organization.vat_country_code = "AT"
    organization.save(update_fields=["billing_name", "billing_address", "vat_country_code"])


def _online_plan_kwargs() -> dict[str, t.Any]:
    return {
        "name": "Monthly Online",
        "price": Decimal("10.00"),
        "currency": "EUR",
        "period_unit": "month",
        "payment_method": MembershipSubscriptionPlan.PaymentMethod.ONLINE,
    }


class TestOnlinePlanPrerequisites:
    def test_create_online_plan_without_stripe_connect_raises(
        self, tier: MembershipTier, organization: Organization
    ) -> None:
        """No Stripe Connect account → the org cannot sell subscriptions online."""
        assert not organization.is_stripe_connected

        with pytest.raises(StripeNotConnectedError):
            subscription_service.create_plan(tier, **_online_plan_kwargs())

    def test_create_online_plan_without_billing_info_raises(
        self, tier: MembershipTier, organization: Organization
    ) -> None:
        """Platform fees apply but billing info is missing → the fee invoice would be unissuable."""
        _connect_stripe(organization)
        assert organization.platform_fee_percent > 0 or organization.platform_fee_fixed > 0
        assert not organization.billing_name

        with pytest.raises(BillingInfoRequiredError):
            subscription_service.create_plan(tier, **_online_plan_kwargs())

    def test_create_online_plan_without_platform_fees_skips_billing_check(
        self, tier: MembershipTier, organization: Organization
    ) -> None:
        """Zero platform fees → no invoice to issue, so incomplete billing info is fine."""
        _connect_stripe(organization)
        organization.platform_fee_percent = Decimal("0")
        organization.platform_fee_fixed = Decimal("0")
        organization.save(update_fields=["platform_fee_percent", "platform_fee_fixed"])

        with (
            mock.patch(
                "events.service.subscription_stripe_service.stripe.Product.create",
                return_value=mock.MagicMock(id="prod_x"),
            ),
            mock.patch(
                "events.service.subscription_stripe_service.stripe.Price.create",
                return_value=mock.MagicMock(id="price_x"),
            ),
        ):
            plan = subscription_service.create_plan(tier, **_online_plan_kwargs())

        assert plan.stripe_price_id == "price_x"

    def test_create_online_plan_with_complete_billing_info_succeeds(
        self, tier: MembershipTier, organization: Organization
    ) -> None:
        """Connected + complete billing info → the plan is created and synced to Stripe."""
        _connect_stripe(organization)
        _set_billing_info(organization)

        with (
            mock.patch(
                "events.service.subscription_stripe_service.stripe.Product.create",
                return_value=mock.MagicMock(id="prod_ok"),
            ),
            mock.patch(
                "events.service.subscription_stripe_service.stripe.Price.create",
                return_value=mock.MagicMock(id="price_ok"),
            ),
        ):
            plan = subscription_service.create_plan(tier, **_online_plan_kwargs())

        assert plan.pk
        assert plan.stripe_product_id == "prod_ok"
        assert plan.stripe_price_id == "price_ok"

    def test_offline_plan_unaffected_by_missing_billing_info(
        self, tier: MembershipTier, organization: Organization
    ) -> None:
        """OFFLINE plans never touch Stripe, so the gate must not fire."""
        assert not organization.is_stripe_connected
        assert not organization.billing_name

        plan = subscription_service.create_plan(
            tier,
            name="Monthly Offline",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
        )

        assert plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE

    def test_update_flipping_offline_to_online_is_gated(
        self, plan: MembershipSubscriptionPlan, organization: Organization
    ) -> None:
        """Flipping an existing OFFLINE plan to ONLINE goes through the same gate."""
        _connect_stripe(organization)

        with pytest.raises(BillingInfoRequiredError):
            subscription_service.update_plan(plan, payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE)


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

    def test_refuses_hard_blacklisted_user(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
        recorder: RevelUser,
    ) -> None:
        """A hard-blacklisted user is refused with 403, mirroring the BANNED guard."""
        Blacklist.objects.create(organization=organization, user=subscriber, created_by=recorder)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.create_subscription(plan, subscriber)
        assert excinfo.value.status_code == 403

    def test_soft_blacklist_does_not_block(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
        recorder: RevelUser,
    ) -> None:
        """A name-only (fuzzy) blacklist entry is not a hard block and must not refuse."""
        Blacklist.objects.create(organization=organization, first_name="Some", last_name="Name", created_by=recorder)
        sub = subscription_service.create_subscription(plan, subscriber)
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

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

    def test_refuses_archived_plan(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        """Subscribing to a plan that has been archived returns 400."""
        subscription_service.archive_plan(plan)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.create_subscription(plan, subscriber)
        assert excinfo.value.status_code == 400

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

    def test_payment_against_terminal_is_refused(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """Terminal subscriptions refuse SUCCEEDED payments outright."""
        sub = subscription_service.create_subscription(plan, subscriber)
        period_before = sub.current_period_end
        sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        sub.save()

        with pytest.raises(HttpError) as excinfo:
            subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        assert excinfo.value.status_code == 400

        sub.refresh_from_db()
        # Status and period stay frozen.
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.current_period_end == period_before
        assert sub.payments.count() == 0

    def test_payment_against_cancelled_is_refused(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(sub, immediate=True)

        with pytest.raises(HttpError) as excinfo:
            subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        assert excinfo.value.status_code == 400

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


class TestRecordPaymentOccurredAt:
    """Backfill semantics for ``occurred_at``."""

    def test_backfill_anchors_period_to_occurred_at(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-03-01 12:00:00"):
            sub = subscription_service.create_subscription(plan, subscriber)

        with freeze_time("2026-03-20 12:00:00"):
            backfill = timezone.now() - datetime.timedelta(days=15)  # 2026-03-05
            payment = subscription_service.record_payment(
                sub,
                amount=Decimal("10.00"),
                currency="EUR",
                recorded_by=recorder,
                occurred_at=backfill,
            )

        assert payment.occurred_at == backfill
        assert payment.period_start == backfill

    def test_occurred_at_in_future_is_rejected(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        future = timezone.now() + datetime.timedelta(days=1)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.record_payment(
                sub,
                amount=Decimal("10.00"),
                currency="EUR",
                recorded_by=recorder,
                occurred_at=future,
            )
        assert excinfo.value.status_code == 400

    def test_occurred_at_predating_subscription_is_rejected(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-03-01 12:00:00"):
            sub = subscription_service.create_subscription(plan, subscriber)
        with freeze_time("2026-03-10 12:00:00"):
            with pytest.raises(HttpError) as excinfo:
                subscription_service.record_payment(
                    sub,
                    amount=Decimal("10.00"),
                    currency="EUR",
                    recorded_by=recorder,
                    occurred_at=timezone.now() - datetime.timedelta(days=30),
                )
            assert excinfo.value.status_code == 400

    def test_occurred_at_predating_lapsed_period_end_is_rejected(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        with freeze_time("2026-01-01 12:00:00"):
            sub = subscription_service.create_subscription(
                plan,
                subscriber,
                initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
            )
            sub.refresh_from_db()

        # Subscription has lapsed: now is well past current_period_end. Trying to backfill
        # to a date before that lapsed period end would create a confusing back-dated
        # active window.
        with freeze_time("2026-06-01 12:00:00"):
            with pytest.raises(HttpError) as excinfo:
                subscription_service.record_payment(
                    sub,
                    amount=Decimal("10.00"),
                    currency="EUR",
                    recorded_by=recorder,
                    occurred_at=timezone.now() - datetime.timedelta(days=121),  # 2026-02-01 - 1d
                )
            assert excinfo.value.status_code == 400

    def test_occurred_at_omitted_uses_now_and_persists_null(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        payment = subscription_service.record_payment(
            sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder
        )
        assert payment.occurred_at is None

    def test_occurred_at_predating_active_period_start_is_rejected(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        # Subscription has an active period [Apr 1, May 1]; today is Apr 15.
        # Backfilling to Mar 15 falls inside an already-paid (prior) window.
        with freeze_time("2026-03-01 12:00:00"):
            sub = subscription_service.create_subscription(
                plan,
                subscriber,
                initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
            )
        # Renew on Apr 1 to advance into [Apr 1, May 1].
        with freeze_time("2026-04-01 12:00:00"):
            subscription_service.record_payment(sub, amount=Decimal("10.00"), currency="EUR", recorded_by=recorder)
        with freeze_time("2026-04-15 12:00:00"):
            sub.refresh_from_db()
            assert sub.current_period_start is not None
            assert sub.current_period_end is not None and sub.current_period_end > timezone.now()
            with pytest.raises(HttpError) as excinfo:
                subscription_service.record_payment(
                    sub,
                    amount=Decimal("10.00"),
                    currency="EUR",
                    recorded_by=recorder,
                    occurred_at=timezone.now() - datetime.timedelta(days=31),  # before current_period_start
                )
            assert excinfo.value.status_code == 400

    def test_succeeded_backfill_producing_lapsed_period_end_is_rejected(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        # Subscription created 6 months ago, never paid (PENDING). Backfilling a
        # SUCCEEDED payment with occurred_at = 5 months ago would produce a period
        # ending ~4 months ago — already lapsed. The status would otherwise revive
        # PENDING -> ACTIVE, leaving an ACTIVE-but-lapsed inconsistency.
        with freeze_time("2026-01-01 12:00:00"):
            sub = subscription_service.create_subscription(plan, subscriber)
            assert sub.current_period_end is None  # never paid

        with freeze_time("2026-07-01 12:00:00"):
            with pytest.raises(HttpError) as excinfo:
                subscription_service.record_payment(
                    sub,
                    amount=Decimal("10.00"),
                    currency="EUR",
                    recorded_by=recorder,
                    occurred_at=timezone.now() - datetime.timedelta(days=150),  # ~Feb 2; +1mo still in past
                )
            assert excinfo.value.status_code == 400


# ---- cancel / pause / resume -------------------------------------------------


class TestLifecycle:
    def test_cancel_at_period_end(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        # The initial payment gives the row a period boundary to cancel at;
        # without one the scheduled cancel is upgraded to an immediate one.
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=subscription_service.InitialPayment(
                amount=plan.price, currency=plan.currency, recorded_by=recorder
            ),
        )
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

    def test_schedule_cancel_blocked_when_paused(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        """A paused subscription cannot be scheduled to cancel at period end.

        Time is frozen while PAUSED, so the period boundary would never be
        reached. Callers must resume first or cancel immediately.
        """
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.cancel_subscription(sub, immediate=False)
        assert excinfo.value.status_code == 400

    def test_cancel_immediate_works_when_paused(self, plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        out = subscription_service.cancel_subscription(sub, immediate=True)
        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_schedule_cancel_blocked_when_paused_online(self, tier: MembershipTier, subscriber: RevelUser) -> None:
        """The PAUSED guard must gate the ONLINE branch too.

        Regression: the guard used to live only in the OFFLINE else-branch, so
        a PAUSED ONLINE subscription could take the scheduled path — leaving
        the row PAUSED with ``cancel_at_period_end=True``, invisible to the
        grace-expiry sweep (which only selects ACTIVE/PAST_DUE).
        """
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online Paused Cancel",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
            stripe_subscription_id="sub_paused_online",
        )
        with pytest.raises(HttpError) as excinfo:
            subscription_service.cancel_subscription(sub, immediate=False)
        assert excinfo.value.status_code == 400
        sub.refresh_from_db()
        assert sub.cancel_at_period_end is False
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_pause_blocked_when_cancel_scheduled(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        recorder: RevelUser,
    ) -> None:
        """The reverse of the PAUSED-cancel guard.

        A subscription with a scheduled cancel cannot be paused — pausing would
        freeze it PAUSED+cancel_at_period_end forever, invisible to the
        grace-expiry sweep (which only selects ACTIVE/PAST_DUE).
        """
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        subscription_service.cancel_subscription(sub, immediate=False)
        with pytest.raises(HttpError) as excinfo:
            subscription_service.pause_subscription(sub)
        assert excinfo.value.status_code == 400
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.cancel_at_period_end is True

    def test_pause_blocked_when_cancel_scheduled_online(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """The scheduled-cancel pause guard gates the ONLINE branch too, before any Stripe call."""
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online Cancel Scheduled",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_cancel_scheduled",
            cancel_at_period_end=True,
        )
        with mock.patch("events.service.subscription_stripe_service.pause_online_subscription") as mock_pause:
            with pytest.raises(HttpError) as excinfo:
                subscription_service.pause_subscription(sub)
        assert excinfo.value.status_code == 400
        mock_pause.assert_not_called()
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.cancel_at_period_end is True

    def test_pause_online_without_stripe_id_is_refused(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        """An ONLINE row that hasn't been linked to a Stripe subscription must not pause locally."""
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        # stripe_subscription_id intentionally empty
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as exc:
            subscription_service.pause_subscription(sub)
        assert exc.value.status_code == 400

    def test_resume_online_without_stripe_id_is_refused(
        self,
        tier: MembershipTier,
        subscriber: RevelUser,
    ) -> None:
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Online R",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PAUSED,
        )
        with pytest.raises(HttpError) as exc:
            subscription_service.resume_subscription(sub)
        assert exc.value.status_code == 400


# ---- refund_payment ----------------------------------------------------------


class TestRefundPayment:
    def test_refund_full_current_period_auto_cancels_subscription(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, recorder: RevelUser
    ) -> None:
        """Phase 4: a full refund of the subscription's current period
        auto-cancels the subscription immediately. The previous record-only
        contract was replaced in Phase 4 (§2 of the design)."""
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=InitialPayment(amount=Decimal("10.00"), currency="EUR", recorded_by=recorder),
        )
        payment = sub.payments.first()
        assert payment is not None

        refunded = subscription_refunds.refund_payment(payment, recorded_by=recorder, notes="customer asked")
        assert refunded.status == MembershipPayment.PaymentStatus.REFUNDED

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert sub.cancelled_at is not None

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
        subscription_refunds.refund_payment(payment, recorded_by=recorder)
        again = subscription_refunds.refund_payment(payment, recorded_by=recorder)
        assert again.status == MembershipPayment.PaymentStatus.REFUNDED
