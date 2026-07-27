"""Tests for the E2E subscription-lifecycle fixtures in ``bootstrap_test_events`` (#795)."""

import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.bootstrap_test_events import ORG_ALPHA_SLUG, Command
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.schema import MySubscriptionSchema

pytestmark = pytest.mark.django_db

REVIVAL_IN = "test.revival.in@example.com"
REVIVAL_OUT = "test.revival.out@example.com"
PAST_DUE = "test.pastdue@example.com"


@pytest.fixture
def org_alpha() -> Organization:
    """Org Alpha as ``bootstrap_events`` seeds it (Stripe-connected, default windows)."""
    owner = RevelUser.objects.create_user(
        username="alice.owner@example.com", email="alice.owner@example.com", password="x"
    )
    return Organization.objects.create(name="Revel Events Collective", slug=ORG_ALPHA_SLUG, owner=owner)


def _run() -> Command:
    """Run only the subscription-fixture pass of the bootstrap command."""
    command = Command()
    command.now = timezone.now()
    command._create_subscription_fixtures()
    return command


def _sub(email: str) -> MembershipSubscription:
    return MembershipSubscription.objects.select_related("organization", "plan__tier").get(user__username=email)


class TestSubscriptionFixtures:
    """Coverage for ``Command._create_subscription_fixtures``."""

    def test_creates_online_revival_plan(self, org_alpha: Organization) -> None:
        _run()

        tier = MembershipTier.objects.get(organization=org_alpha, name="E2E Revival Tier")
        plan = MembershipSubscriptionPlan.objects.get(tier=tier, name="E2E Revival Plan")
        assert plan.price == Decimal("10.00")
        assert plan.currency == "EUR"
        assert plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH
        assert plan.period_count == 1
        assert plan.is_active
        assert plan.sales_status == MembershipSubscriptionPlan.SalesStatus.OPEN
        assert plan.max_subscriptions is None
        assert plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE

    def test_creates_verified_users_with_shared_password(self, org_alpha: Organization) -> None:
        _run()

        for email in (REVIVAL_IN, REVIVAL_OUT, PAST_DUE):
            user = RevelUser.objects.get(username=email)
            assert user.email == email
            assert user.email_verified
            assert user.check_password("password123")

    def test_revival_in_window_is_revivable(self, org_alpha: Organization) -> None:
        command = _run()
        subscription = _sub(REVIVAL_IN)

        assert subscription.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert subscription.expired_at is not None
        assert (command.now - subscription.expired_at).days == 5
        # Inside the org's default 30-day window → a deadline ~25 days out.
        deadline = MySubscriptionSchema.resolve_revival_deadline(subscription)
        assert deadline is not None
        assert deadline > command.now
        assert (deadline - command.now).days == 25
        # Revive mints a fresh checkout session, so no live Stripe record is needed.
        assert subscription.stripe_subscription_id is None
        assert subscription.stripe_checkout_session_id == ""

    def test_revival_in_window_has_payment_ledger(self, org_alpha: Organization) -> None:
        """The in-window row carries a payment so an abandoned revival checkout reverts to EXPIRED (#802).

        ``_clear_stale_pending_checkout`` deletes payment-less PENDING rows; with a
        ledger entry it reverts them instead, keeping the revive E2E spec re-runnable.
        """
        _run()
        subscription = _sub(REVIVAL_IN)

        payment = MembershipPayment.objects.get(subscription=subscription)
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert subscription.plan is not None
        assert payment.amount == subscription.plan.price
        assert payment.currency == "EUR"
        assert payment.period_start == subscription.current_period_start
        assert payment.period_end == subscription.current_period_end

    def test_revival_out_of_window(self, org_alpha: Organization) -> None:
        command = _run()
        subscription = _sub(REVIVAL_OUT)

        assert subscription.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert subscription.expired_at is not None
        assert (command.now - subscription.expired_at).days == 60
        deadline = MySubscriptionSchema.resolve_revival_deadline(subscription)
        assert deadline is not None
        assert deadline < command.now

    def test_past_due_grace_deadline_is_in_the_future(self, org_alpha: Organization) -> None:
        command = _run()
        subscription = _sub(PAST_DUE)

        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        assert subscription.expired_at is None
        assert subscription.current_period_end is not None
        assert subscription.current_period_end < command.now
        grace_deadline = MySubscriptionSchema.resolve_grace_deadline(subscription)
        assert grace_deadline is not None
        assert grace_deadline > command.now

    def test_membership_status_mirrors_subscription(self, org_alpha: Organization) -> None:
        """The post_save signal syncs the seeded memberships: cancelled when expired, active in grace."""
        _run()

        for email in (REVIVAL_IN, REVIVAL_OUT):
            member = OrganizationMember.objects.get(organization=org_alpha, user__username=email)
            assert member.status == OrganizationMember.MembershipStatus.CANCELLED

        past_due_member = OrganizationMember.objects.get(organization=org_alpha, user__username=PAST_DUE)
        assert past_due_member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert past_due_member.tier is not None
        assert past_due_member.tier.name == "E2E Revival Tier"

    def test_is_idempotent(self, org_alpha: Organization) -> None:
        """The canonical reseed order re-runs the command; a second pass must not duplicate or raise."""
        _run()
        _run()  # must not raise

        assert MembershipTier.objects.filter(organization=org_alpha, name="E2E Revival Tier").count() == 1
        assert MembershipSubscriptionPlan.objects.filter(name="E2E Revival Plan").count() == 1
        for email in (REVIVAL_IN, REVIVAL_OUT, PAST_DUE):
            assert RevelUser.objects.filter(username=email).count() == 1
            assert MembershipSubscription.objects.filter(user__username=email).count() == 1
            assert OrganizationMember.objects.filter(user__username=email).count() == 1
        assert MembershipPayment.objects.filter(subscription__user__username=REVIVAL_IN).count() == 1

    def test_skips_when_org_alpha_missing(self, db: t.Any) -> None:
        """``bootstrap_test_events`` must not explode when demo data hasn't been seeded."""
        _run()  # must not raise

        assert not MembershipSubscription.objects.exists()
        assert not RevelUser.objects.filter(username=REVIVAL_IN).exists()
