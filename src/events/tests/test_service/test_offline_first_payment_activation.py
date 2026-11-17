"""First SUCCEEDED payment on an OFFLINE plan activates a held member (and only then)."""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SubscriptionPlanChangeRefusedError
from events.models import (
    Blacklist,
    MembershipPayment,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import payment_edits
from events.service.subscription import plans as subscription_plans
from notifications.enums import NotificationType
from notifications.signals import notification_requested

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def gold(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def plan(gold: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        gold, name="Annual", price=Decimal("50.00"), currency="EUR", period_unit="year", period_count=1
    )


@pytest.fixture
def sent() -> t.Iterator[list[NotificationType]]:
    """Notification types dispatched while the test runs (delivery itself is mocked out)."""
    types: list[NotificationType] = []
    with mock.patch.object(
        notification_requested, "send", side_effect=lambda *a, **kw: types.append(kw["notification_type"])
    ):
        yield types


def _pending_sub(plan: MembershipSubscriptionPlan, user: RevelUser) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=user,
        organization=plan.tier.organization,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )


def _pay(sub: MembershipSubscription, **kw: t.Any) -> MembershipPayment:
    return subscription_lifecycle.record_payment(sub, amount=Decimal("50.00"), currency="EUR", recorded_by=None, **kw)


def _member(sub: MembershipSubscription) -> OrganizationMember:
    return OrganizationMember.objects.get(organization=sub.organization, user=sub.user)


def test_awaiting_member_activates_and_granted_once(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        status=S.AWAITING_PAYMENT,
        tier=MembershipTier.objects.get(organization=organization, name="General membership"),
    )
    sub = _pending_sub(plan, member_user)
    assert _member(sub).status == S.AWAITING_PAYMENT  # saving the PENDING sub did not activate it
    assert not MembershipRecord.objects.filter(user=member_user).exists()

    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)

    member = _member(sub)
    assert member.status == S.ACTIVE
    assert member.tier_id == plan.tier_id
    assert MembershipRecord.objects.filter(user=member_user, valid_until__isnull=True).exists()
    assert sent.count(NotificationType.MEMBERSHIP_GRANTED) == 1
    assert NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED not in sent
    assert NotificationType.MEMBERSHIP_CARD_UPDATED not in sent


def test_existing_member_keeps_tier_until_first_payment_then_receipt(
    plan: MembershipSubscriptionPlan,
    tier: MembershipTier,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.ACTIVE, tier=tier)
    sub = _pending_sub(plan, member_user)
    assert _member(sub).tier_id == tier.pk

    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)

    assert _member(sub).tier_id == plan.tier_id
    assert _member(sub).status == S.ACTIVE
    assert sent.count(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED) == 1
    assert (
        NotificationType.MEMBERSHIP_GRANTED not in sent[sent.index(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED) :]
    )


def test_legacy_active_member_untouched(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """ACTIVE member at the plan's tier + never-paid PENDING sub: no downgrade; first payment is a receipt."""
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.ACTIVE, tier=plan.tier)
    sub = _pending_sub(plan, member_user)
    sub.save()  # a re-save must not move the member
    assert _member(sub).status == S.ACTIVE

    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)

    assert _member(sub).status == S.ACTIVE
    assert sent == [NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED]


def test_paused_member_left_alone(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.PAUSED)
    sub = _pending_sub(plan, member_user)
    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)
    assert _member(sub).status == S.PAUSED
    assert NotificationType.MEMBERSHIP_GRANTED not in sent
    assert NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED not in sent


def test_second_payment_does_not_reactivate(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)
    # Staff pause the member between payments; a second payment must neither lift it nor re-run activation.
    OrganizationMember.objects.filter(pk=_member(sub).pk).update(status=S.PAUSED)
    sent.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)

    assert _member(sub).status == S.PAUSED
    assert NotificationType.MEMBERSHIP_GRANTED not in sent
    assert sent.count(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED) == 1  # an ordinary renewal receipt only


def test_blacklisted_first_payment_400_no_row(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    with mock.patch("events.service.subscription.offline.check_user_hard_blacklisted", return_value=True):
        with pytest.raises(HttpError) as exc:
            _pay(sub)
    assert exc.value.status_code == 400
    assert "blacklisted" in str(exc.value.message)
    assert not MembershipPayment.objects.filter(subscription=sub).exists()
    assert _member(sub).status == S.AWAITING_PAYMENT
    assert Blacklist.objects.count() == 0  # real blacklist rows are not needed to exercise the guard


def test_edit_to_succeeded_blacklisted_400(
    plan: MembershipSubscriptionPlan, organization: Organization, member_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    payment = _pay(sub, status=MembershipPayment.PaymentStatus.FAILED)
    with mock.patch("events.service.subscription.offline.check_user_hard_blacklisted", return_value=True):
        with pytest.raises(HttpError) as exc:
            payment_edits.update_payment(
                payment, changes={"status": MembershipPayment.PaymentStatus.SUCCEEDED}, recorded_by=None
            )
    assert exc.value.status_code == 400
    payment.refresh_from_db()
    assert payment.status == MembershipPayment.PaymentStatus.FAILED
    assert _member(sub).status == S.AWAITING_PAYMENT


def test_settles_linked_application(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    application = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=member_user,
        tier=plan.tier,
        plan=plan,
        subscription=sub,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    with django_capture_on_commit_callbacks(execute=True):
        _pay(sub)
    application.refresh_from_db()
    assert application.status == OrganizationMembershipRequest.Status.COMPLETED


def test_edit_failed_to_succeeded_activates(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    member_user: RevelUser,
    sent: list[NotificationType],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    payment = _pay(sub, status=MembershipPayment.PaymentStatus.FAILED)
    assert _member(sub).status == S.AWAITING_PAYMENT

    with django_capture_on_commit_callbacks(execute=True):
        payment_edits.update_payment(
            payment, changes={"status": MembershipPayment.PaymentStatus.SUCCEEDED}, recorded_by=None
        )

    assert _member(sub).status == S.ACTIVE
    assert sent.count(NotificationType.MEMBERSHIP_GRANTED) == 1
    sub.refresh_from_db()
    assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE


def test_staff_change_plan_never_paid_offline_to_free_refused(
    plan: MembershipSubscriptionPlan, organization: Organization, member_user: RevelUser, gold: MembershipTier
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)
    sub = _pending_sub(plan, member_user)
    free_plan = subscription_plans.create_plan(
        gold,
        name="Free",
        price=Decimal("0"),
        currency="EUR",
        period_unit="lifetime",
        payment_method=MembershipSubscriptionPlan.PaymentMethod.FREE,
    )
    with pytest.raises(SubscriptionPlanChangeRefusedError):
        subscription_lifecycle.staff_change_plan(sub, free_plan)
    assert _member(sub).status == S.AWAITING_PAYMENT
