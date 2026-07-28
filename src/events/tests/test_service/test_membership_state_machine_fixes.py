"""Regression tests for the membership state-machine fixes from the #774 review.

Three holes, one theme: a staff decision about *who is a member* (a ban, a
pause) could be undone by a path that only meant to talk about the
subscription — and a subscription that could never reach a period boundary
could be scheduled to cancel there anyway.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service import subscription_service, subscription_uncancel
from events.service.organization_service import approve_membership_request, update_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """An OFFLINE monthly plan, capped at one subscription so slots are observable."""
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
    return django_user_model.objects.create_user(
        username="state_machine_sub", email="state-machine-sub@example.com", password="pass"
    )


class TestStaffApprovalCannotUnban:
    """2a — approving a stale free application must not resurrect a BANNED member."""

    def _pending_application(self, organization: Organization, user: RevelUser) -> OrganizationMembershipRequest:
        return OrganizationMembershipRequest.objects.create(organization=organization, user=user)

    @pytest.mark.parametrize("force", [False, True])
    def test_banned_member_is_refused(
        self,
        organization: Organization,
        subscriber: RevelUser,
        tier: MembershipTier,
        force: bool,
    ) -> None:
        """``force`` comps a paying member; it must never lift a ban."""
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        application = self._pending_application(organization, subscriber)

        with pytest.raises(HttpError) as exc_info:
            approve_membership_request(application, organization.owner, tier, force=force)

        assert exc_info.value.status_code == 403
        application.refresh_from_db()
        assert application.status == OrganizationMembershipRequest.Status.PENDING
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    @pytest.mark.parametrize("force", [False, True])
    def test_hard_blacklisted_user_is_refused(
        self,
        organization: Organization,
        subscriber: RevelUser,
        tier: MembershipTier,
        force: bool,
    ) -> None:
        """Same helper the Stripe sync uses — no member row is minted for a blacklisted user."""
        Blacklist.objects.create(
            organization=organization,
            email=subscriber.email,
            created_by=organization.owner,
        )
        application = self._pending_application(organization, subscriber)

        with pytest.raises(HttpError) as exc_info:
            approve_membership_request(application, organization.owner, tier, force=force)

        assert exc_info.value.status_code == 403
        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()

    def test_ordinary_approval_still_grants_membership(
        self,
        organization: Organization,
        subscriber: RevelUser,
        tier: MembershipTier,
    ) -> None:
        application = self._pending_application(organization, subscriber)

        approve_membership_request(application, organization.owner, tier)

        application.refresh_from_db()
        assert application.status == OrganizationMembershipRequest.Status.COMPLETED
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == tier.id


class TestPausedMemberCannotSelfResume:
    """2b — a staff pause survives every subscription-side write until staff lift it."""

    def _paid_subscription(
        self, plan: MembershipSubscriptionPlan, subscriber: RevelUser, organization: Organization
    ) -> MembershipSubscription:
        """An ACTIVE OFFLINE subscription with a real period boundary."""
        return subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=subscription_service.InitialPayment(
                amount=plan.price, currency=plan.currency, recorded_by=organization.owner
            ),
        )

    def test_uncancel_is_refused_while_the_membership_is_paused(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        subscription = self._paid_subscription(plan, subscriber, organization)
        subscription_service.cancel_subscription(subscription, immediate=False)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        with pytest.raises(HttpError) as exc_info:
            subscription_uncancel.uncancel_subscription(subscription)

        assert exc_info.value.status_code == 403
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_uncancel_is_refused_while_the_membership_is_banned(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        subscription = self._paid_subscription(plan, subscriber, organization)
        subscription_service.cancel_subscription(subscription, immediate=False)
        OrganizationMember.objects.filter(organization=organization, user=subscriber).update(
            status=OrganizationMember.MembershipStatus.BANNED
        )

        with pytest.raises(HttpError) as exc_info:
            subscription_uncancel.uncancel_subscription(subscription)

        assert exc_info.value.status_code == 403

    def test_resaving_an_active_subscription_does_not_lift_the_pause(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        """The webhook echo case: any save of a still-ACTIVE row used to un-pause the member."""
        subscription = self._paid_subscription(plan, subscriber, organization)
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

        # A period bump, exactly as ``sync_subscription_from_stripe`` would write it.
        subscription.current_period_end = timezone.now() + timedelta(days=60)
        subscription.save(update_fields=["current_period_end", "updated_at"])

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_staff_resume_of_a_paused_subscription_reactivates_the_member(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        subscription = self._paid_subscription(plan, subscriber, organization)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED

        subscription_service.resume_subscription(subscription)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_staff_can_still_unpause_the_member_directly(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        subscription = self._paid_subscription(plan, subscriber, organization)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        update_member(member, status=OrganizationMember.MembershipStatus.ACTIVE)

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        # Billing stays paused until staff resume it explicitly — ACTIVE is
        # deliberately not mirrored (see ``_mirror_status_to_subscriptions``).
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_banned_members_are_still_shielded(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        subscription = self._paid_subscription(plan, subscriber, organization)
        OrganizationMember.objects.filter(organization=organization, user=subscriber).update(
            status=OrganizationMember.MembershipStatus.BANNED
        )

        subscription.current_period_end = timezone.now() + timedelta(days=60)
        subscription.save(update_fields=["current_period_end", "updated_at"])

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.BANNED


class TestScheduledCancelOfAPeriodLessRow:
    """2c — a row with no period boundary must not be left waiting for one."""

    def test_pending_offline_row_is_cancelled_immediately(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization: Organization,
    ) -> None:
        """Staff-created, never paid: the scheduled cancel has no boundary to land on."""
        subscription = subscription_service.create_subscription(plan, subscriber)
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert subscription.current_period_end is None
        assert plan.occupied_slot_count() == 1

        out = subscription_service.cancel_subscription(subscription, immediate=False)

        assert out.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert out.cancelled_at is not None
        assert out.cancel_at_period_end is False
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED
        assert plan.occupied_slot_count() == 0
