"""Tests for the MembershipSubscription -> OrganizationMember sync signal."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

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

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def pro_tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="sub_signal", email="signal@example.com", password="pass")


class TestSyncMemberFromSubscription:
    def test_does_not_create_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """Creating a subscription directly (bypassing service) must not create a member via signal."""
        sub = MembershipSubscription.objects.create(user=subscriber, plan=plan, organization=organization)
        # The signal fires on save but only updates existing members.
        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_active_member_unchanged_when_status_already_matches(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.ACTIVE,
            tier=tier,
        )
        subscription_service.create_subscription(plan, subscriber)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_subscription_tier_wins(
        self,
        organization: Organization,
        subscriber: RevelUser,
        pro_tier: MembershipTier,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=MembershipTier.objects.get(organization=organization, name="General membership"),
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        pro_plan = subscription_service.create_plan(
            pro_tier, name="Pro Monthly", price=Decimal("20.00"), currency="EUR", period_unit="month"
        )
        subscription_service.create_subscription(pro_plan, subscriber)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.tier_id == pro_tier.pk

    def test_banned_member_is_never_overwritten(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        # Bypass service refusal: directly create the subscription row so we can
        # confirm the signal alone does not overwrite BANNED.
        sub = MembershipSubscription.objects.create(user=subscriber, plan=plan, organization=organization)
        sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        sub.save()
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    def test_expired_subscription_cancels_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        sub.save()
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_paused_subscription_pauses_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_resumed_subscription_reactivates_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """Subscription-driven pause → resume must still flip the member back to ACTIVE."""
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        assert (
            OrganizationMember.objects.get(organization=organization, user=subscriber).status
            == OrganizationMember.MembershipStatus.PAUSED
        )

        subscription_service.resume_subscription(sub)

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_stale_terminal_subscription_does_not_overwrite_active(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """Re-saving an older terminal subscription must not clobber the current one.

        Scenario: user subscribes (Sub1), cancels, then resubscribes (Sub2).
        Sub1 is later re-saved (e.g. via admin edit). The signal must not
        flip the member back to CANCELLED because Sub2 owns the state.
        """
        old_sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(old_sub, immediate=True)
        subscription_service.create_subscription(plan, subscriber)

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

        # Re-saving the old (terminal) subscription must NOT touch the member.
        # Refresh first so the local copy reflects the CANCELLED state — otherwise
        # the stale PENDING status would violate the partial-unique constraint.
        old_sub.refresh_from_db()
        old_sub.save()
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE


@pytest.fixture
def online_pro_plan(pro_tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A paid ONLINE plan on a tier the subscriber does not already hold."""
    return MembershipSubscriptionPlan.objects.create(
        tier=pro_tier,
        name="Pro Monthly Online",
        price=Decimal("20.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_signal_gate",
        stripe_price_id="price_signal_gate",
    )


def _fail_invoice(sub: MembershipSubscription) -> None:
    """What ``_apply_invoice_outcome`` does when an invoice fails: → PAST_DUE."""
    sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
    sub.save(update_fields=["status", "updated_at"])


class TestOnlinePastDuePrepaymentGate:
    """A PAST_DUE ONLINE row that never collected a period must not grant access.

    ``_apply_invoice_outcome`` moves PENDING → PAST_DUE on a *failed* first
    invoice (SCA stalls route through the same branch), so the PAST_DUE →
    member-ACTIVE mapping — meant as dunning grace for paying members — would
    otherwise hand out the paid tier before any money moved.
    """

    def test_failed_first_invoice_does_not_create_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        sub = MembershipSubscription.objects.create(user=subscriber, plan=online_pro_plan, organization=organization)

        _fail_invoice(sub)

        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()

    def test_failed_first_invoice_does_not_upgrade_free_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        tier: MembershipTier,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A free-tier member subscribing to a paid plan keeps their tier until they pay."""
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        sub = MembershipSubscription.objects.create(user=subscriber, plan=online_pro_plan, organization=organization)

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.tier_id == tier.pk
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_failed_first_invoice_with_mirrored_period_does_not_upgrade(
        self,
        organization: Organization,
        subscriber: RevelUser,
        tier: MembershipTier,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A period alone is not proof of payment.

        ``customer.subscription.*`` mirrors Stripe's period onto a still
        ``incomplete`` (locally PENDING) row, so the row can carry
        ``current_period_end`` while nothing has been collected.
        """
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        now = timezone.now()
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_pro_plan,
            organization=organization,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.tier_id == tier.pk

    def test_failed_revival_invoice_does_not_reactivate_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A revival reuses the row (and its payment history) with the period reset."""
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=online_pro_plan.tier,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        now = timezone.now()
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_pro_plan,
            organization=organization,
            expired_at=now - timedelta(days=10),
        )
        # The previous life's payment: it covers a period that ended before the
        # revival, so it must not vouch for the new one.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("20.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=now - timedelta(days=70),
            period_end=now - timedelta(days=40),
        )

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_renewal_dunning_keeps_member_active(
        self,
        organization: Organization,
        subscriber: RevelUser,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        """The grace path is untouched: a paid-up row lapsing into PAST_DUE keeps access."""
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=online_pro_plan.tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        now = timezone.now()
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_pro_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(minutes=1),
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("20.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=now - timedelta(days=30),
            period_end=now - timedelta(minutes=1),
        )

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == online_pro_plan.tier_id

    def test_renewal_dunning_still_syncs_a_paid_row(
        self,
        organization: Organization,
        subscriber: RevelUser,
        online_pro_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A paid row is *not* gated: PAST_DUE still writes the member state through."""
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=online_pro_plan.tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        now = timezone.now()
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_pro_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(minutes=1),
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("20.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=now - timedelta(days=30),
            period_end=now - timedelta(minutes=1),
        )
        # Drift the member out of the state the subscription implies, so the
        # PAST_DUE sync has something observable to write.
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        member.save(update_fields=["status"])

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_offline_past_due_is_not_gated(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
    ) -> None:
        """OFFLINE plans have no Stripe pre-payment gate — behavior is unchanged."""
        # Subscription first: the signal never *creates* a member, so this
        # leaves the member row below in the state the test sets.
        sub = MembershipSubscription.objects.create(user=subscriber, plan=plan, organization=organization)
        member = OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=tier,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        _fail_invoice(sub)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE


class TestModelIntegrity:
    def test_subscription_org_must_match_plan_org(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization_owner_user: RevelUser,
    ) -> None:
        other_org = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
        sub = MembershipSubscription(user=subscriber, plan=plan, organization=other_org)
        with pytest.raises(ValidationError):
            sub.save()
