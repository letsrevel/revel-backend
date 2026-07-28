"""An OFFLINE revival must leave the subscriber with an actual membership.

Regression: ``revive_subscription``'s OFFLINE branch flipped the row to ACTIVE
and recorded the payment, relying on ``sync_member_from_subscription`` — which
by design never *creates* an :class:`OrganizationMember`. ``remove_member``
deletes the member row while leaving a terminal EXPIRED subscription behind
(``cancel_subscriptions_for_membership_loss`` skips terminal rows), so a staff
revival after a cash payment produced an ACTIVE subscriber with no membership,
no tier and no access — and nothing downstream repaired it.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import organization_service, subscription_service
from events.service.subscription_service import InitialPayment


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
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
    return django_user_model.objects.create_user(
        username="revmem_user", email="revmem_user@example.com", password="pass"
    )


@pytest.fixture
def staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="revmem_staff", email="revmem_staff@example.com", password="pass"
    )


@pytest.fixture
def expired_sub(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    subscriber: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.EXPIRED,
        expired_at=timezone.now() - timedelta(days=5),
    )


@pytest.fixture
def payload(plan: MembershipSubscriptionPlan, staff_user: RevelUser) -> InitialPayment:
    return InitialPayment(amount=plan.price, currency=plan.currency, recorded_by=staff_user)


@pytest.mark.django_db
class TestOfflineRevivalMaterializesMember:
    def test_creates_member_when_row_is_missing(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
    ) -> None:
        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()

        revived, url = subscription_service.revive_subscription(
            expired_sub,
            initial_payment=payload,
            revived_by=staff_user,
            enforce_sales_status=False,
        )

        assert url is None
        assert revived.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == plan.tier_id

    def test_revive_after_remove_member_restores_access(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
    ) -> None:
        """The end-to-end path that produced the ghost subscriber.

        ``remove_member`` only cancels *non-terminal* subscriptions, so the
        EXPIRED row survives its own member row being deleted.
        """
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=plan.tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        organization_service.remove_member(organization, subscriber)
        expired_sub.refresh_from_db()
        assert expired_sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()

        subscription_service.revive_subscription(
            expired_sub,
            initial_payment=payload,
            revived_by=staff_user,
            enforce_sales_status=False,
        )

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier_id == plan.tier_id

    def test_reactivates_cancelled_member_row(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=plan.tier,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )

        subscription_service.revive_subscription(
            expired_sub,
            initial_payment=payload,
            revived_by=staff_user,
            enforce_sales_status=False,
        )

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_banned_member_is_still_refused(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        payload: InitialPayment,
    ) -> None:
        """A BANNED row must neither be revived nor resurrected as ACTIVE."""
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.BANNED,
        )

        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(
                expired_sub,
                initial_payment=payload,
                revived_by=staff_user,
                enforce_sales_status=False,
            )

        assert ei.value.status_code == 403
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.BANNED
        expired_sub.refresh_from_db()
        assert expired_sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED

    def test_paused_member_stays_paused(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
    ) -> None:
        """A staff-imposed suspension outranks the revival."""
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=plan.tier,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )

        subscription_service.revive_subscription(
            expired_sub,
            initial_payment=payload,
            revived_by=staff_user,
            enforce_sales_status=False,
        )

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.PAUSED
