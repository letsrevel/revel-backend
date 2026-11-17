"""``MembershipSubscription.held_member`` and the row locks around offline activation / application cancel."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service.membership_manager import service as membership_manager_service
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans
from events.service.subscription.core import create_subscription
from events.service.subscription.offline import activate_on_first_payment, open_offline_subscription
from events.tasks.subscriptions import expire_unpaid_offline_subscriptions
from events.utils import subscription_payment
from events.utils.subscription_payment import APPLICATION_PAYMENT_WINDOW_DAYS

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus
Sub = MembershipSubscription.SubscriptionStatus
Omr = OrganizationMembershipRequest.Status


@pytest.fixture
def gold(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def plan(gold: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        gold, name="Annual", price=Decimal("50.00"), currency="EUR", period_unit="year", period_count=1
    )


def _member(sub: MembershipSubscription) -> OrganizationMember:
    return OrganizationMember.objects.get(organization=sub.organization, user=sub.user)


def _age(sub: MembershipSubscription, days: int) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(created_at=timezone.now() - timedelta(days=days))


def test_fresh_join_holds_member_and_cancel_releases_it(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    sub = open_offline_subscription(plan, nonmember_user)
    assert sub.held_member is True

    subscription_lifecycle.cancel_subscription(sub)

    assert _member(sub).status == S.CANCELLED


def test_existing_active_same_tier_member_survives_cancel(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    """Staff open an unpaid OFFLINE sub for an already ACTIVE member at the same tier: cancelling keeps them."""
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=plan.tier)
    sub = create_subscription(plan, nonmember_user)
    assert sub.held_member is False

    subscription_lifecycle.cancel_subscription(sub)

    assert _member(sub).status == S.ACTIVE


def test_sweep_expires_sub_but_keeps_preexisting_same_tier_member(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=plan.tier)
    sub = create_subscription(plan, nonmember_user)
    _age(sub, APPLICATION_PAYMENT_WINDOW_DAYS + 1)

    assert expire_unpaid_offline_subscriptions() == 1

    sub.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    assert _member(sub).status == S.ACTIVE


def test_sweep_rechecks_legacy_member_under_lock(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    """A legacy row's member moved onto the plan's tier after the candidate query is no longer swept."""
    free = MembershipTier.objects.get(organization=organization, name="General membership")
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=free)
    legacy = MembershipSubscription.objects.create(
        user=nonmember_user, organization=organization, plan=plan, status=Sub.PENDING
    )
    assert legacy.held_member is None
    _age(legacy, APPLICATION_PAYMENT_WINDOW_DAYS + 1)
    real = subscription_payment.is_never_paid_offline

    def move_member_then_check(sub: MembershipSubscription) -> bool:
        OrganizationMember.objects.filter(organization=organization, user=nonmember_user).update(tier=plan.tier)
        return real(sub)

    with mock.patch.object(subscription_payment, "is_never_paid_offline", side_effect=move_member_then_check):
        assert expire_unpaid_offline_subscriptions() == 0

    legacy.refresh_from_db()
    assert legacy.status == Sub.PENDING
    assert _member(legacy).status == S.ACTIVE


def test_activate_on_first_payment_locks_member_row(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    sub = open_offline_subscription(plan, nonmember_user)
    OrganizationMember.objects.filter(organization=sub.organization, user=nonmember_user).update(status=S.PAUSED)

    with CaptureQueriesContext(connection) as ctx:
        activate_on_first_payment(sub)

    locking = [
        q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"] and "events_organizationmember" in q["sql"]
    ]
    assert locking
    assert _member(sub).status == S.PAUSED  # left alone


def test_cancel_application_uses_locked_row(plan: MembershipSubscriptionPlan, nonmember_user: RevelUser) -> None:
    """A stale in-memory application (read before a subscription got linked) still cancels the linked sub."""
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user,
        organization=plan.tier.organization,
        tier=plan.tier,
        plan=plan,
        status=Omr.PENDING,
    )
    stale = OrganizationMembershipRequest.objects.get(pk=application.pk)
    sub = open_offline_subscription(plan, nonmember_user, application=application)
    assert stale.subscription_id is None

    result = membership_manager_service.cancel_application(stale)

    sub.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    assert result.status == Omr.CANCELLED
    assert result.subscription_id == sub.pk


def test_cancel_application_is_noop_when_row_already_terminal(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user,
        organization=plan.tier.organization,
        tier=plan.tier,
        plan=plan,
        status=Omr.PENDING,
    )
    stale = OrganizationMembershipRequest.objects.get(pk=application.pk)
    OrganizationMembershipRequest.objects.filter(pk=application.pk).update(status=Omr.REJECTED)

    result = membership_manager_service.cancel_application(stale)

    assert result.status == Omr.REJECTED
    application.refresh_from_db()
    assert application.status == Omr.REJECTED


def test_cancel_application_cancels_linked_sub_and_application(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=plan.tier.organization, tier=plan.tier, plan=plan, status=Omr.PENDING
    )
    sub = open_offline_subscription(plan, nonmember_user, application=application)

    result = membership_manager_service.cancel_application(application)

    sub.refresh_from_db()
    assert (sub.status, result.status) == (Sub.CANCELLED, Omr.CANCELLED)
    assert _member(sub).status == S.CANCELLED


def test_cancel_application_locks_subscription_before_application(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    """Lock order is subscription -> application, matching record_payment and the sweep (no deadlock)."""
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=plan.tier.organization, tier=plan.tier, plan=plan, status=Omr.PENDING
    )
    open_offline_subscription(plan, nonmember_user, application=application)

    with CaptureQueriesContext(connection) as ctx:
        membership_manager_service.cancel_application(application)

    locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
    first_sub = next(i for i, q in enumerate(locks) if 'FROM "events_membershipsubscription"' in q)
    first_app = next(i for i, q in enumerate(locks) if 'FROM "events_organizationmembershiprequest"' in q)
    assert first_sub < first_app


def test_cancel_application_retries_when_subscription_linked_after_first_read(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    """The unlocked read saw no link; a concurrent link lands before the application lock: lock it and retry."""
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=plan.tier.organization, tier=plan.tier, plan=plan, status=Omr.PENDING
    )
    sub = open_offline_subscription(plan, nonmember_user, application=application)
    real = membership_manager_service._linked_subscription_id

    with mock.patch.object(membership_manager_service, "_linked_subscription_id", return_value=None) as stale:
        with CaptureQueriesContext(connection) as ctx:
            result = membership_manager_service.cancel_application(application)
    assert stale.call_count == 1
    assert real(application.pk) == sub.pk

    sub.refresh_from_db()
    assert (sub.status, result.status) == (Sub.CANCELLED, Omr.CANCELLED)
    locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
    # first attempt locks only the application; the retry locks the subscription first
    from_sub = 'FROM "events_membershipsubscription"'
    assert from_sub not in locks[0]
    assert from_sub in locks[1]


def test_held_member_moved_to_other_tier_is_left_alone(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    """held_member=True, but staff moved the member to another tier by hand: sub is swept, member untouched."""
    sub = open_offline_subscription(plan, nonmember_user)
    assert sub.held_member is True
    other = MembershipTier.objects.get(organization=organization, name="General membership")
    OrganizationMember.objects.filter(organization=organization, user=nonmember_user).update(
        status=S.ACTIVE, tier=other
    )
    _age(sub, APPLICATION_PAYMENT_WINDOW_DAYS + 1)

    assert expire_unpaid_offline_subscriptions() == 1

    sub.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    member = _member(sub)
    assert (member.status, member.tier_id) == (S.ACTIVE, other.pk)
