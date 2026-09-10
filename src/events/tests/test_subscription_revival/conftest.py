"""Shared fixtures for the EXPIRED → ACTIVE revival tests."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription.lifecycle import InitialPayment


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
    return django_user_model.objects.create_user(username="rev_user", email="rev_user@example.com", password="pass")


@pytest.fixture
def staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="rev_staff", email="rev_staff@example.com", password="pass")


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
    return InitialPayment(
        amount=plan.price,
        currency=plan.currency,
        recorded_by=staff_user,
    )


def _apply_revival_refusal(refusal: str, organization: Organization, user: RevelUser, staff: RevelUser) -> None:
    """Put ``user`` into the BANNED or hard-blacklisted state named by ``refusal``.

    The blacklist variant matches by email on purpose: a user-FK entry also flips
    the member row to BANNED, which would short-circuit the branch under test.
    """
    if refusal == "banned":
        OrganizationMember.objects.create(
            user=user, organization=organization, status=OrganizationMember.MembershipStatus.BANNED
        )
        return
    Blacklist.objects.create(organization=organization, email=user.email, created_by=staff)
