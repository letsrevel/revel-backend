"""Admin subscriptions list: ``payment_method`` filter and ``payment_due_by`` (one query, no N+1)."""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    SubscriptionPaymentMethod,
)
from events.utils.subscription_payment import APPLICATION_PAYMENT_WINDOW_DAYS

pytestmark = pytest.mark.django_db

Sub = MembershipSubscription.SubscriptionStatus


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


def _plan(tier: MembershipTier, method: str) -> MembershipSubscriptionPlan:
    free = method == SubscriptionPaymentMethod.FREE
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=f"{method} plan",
        price=Decimal("0") if free else Decimal("10.00"),
        currency="EUR",
        period_unit="lifetime" if free else "month",
        payment_method=method,
    )


def _sub(
    organization: Organization, plan: MembershipSubscriptionPlan, user: RevelUser, status: str = Sub.PENDING
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(organization=organization, user=user, plan=plan, status=status)


def _users(count: int) -> list[RevelUser]:
    return [
        RevelUser.objects.create_user(username=f"u{i}@example.com", email=f"u{i}@example.com", password="x")
        for i in range(count)
    ]


def _assert_same_instant(rendered: str, expected: datetime) -> None:
    """The API renders milliseconds only, so compare parsed values within a millisecond."""
    assert abs(datetime.fromisoformat(rendered) - expected) < timedelta(milliseconds=1)


def _list(client: Client, organization: Organization, **params: str) -> list[dict[str, t.Any]]:
    response = client.get(reverse("api:list_subscriptions", kwargs={"slug": organization.slug}), params)
    assert response.status_code == 200, response.content
    return t.cast(list[dict[str, t.Any]], response.json()["results"])


def test_payment_method_filter(
    organization_owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    plans = {m: _plan(tier, m) for m in SubscriptionPaymentMethod.values}
    for user, plan in zip(_users(len(plans)), plans.values(), strict=True):
        _sub(organization, plan, user)

    assert len(_list(organization_owner_client, organization)) == 3
    for method, plan in plans.items():
        rows = _list(organization_owner_client, organization, payment_method=method)
        assert [r["plan"]["id"] for r in rows] == [str(plan.id)]


def test_payment_method_filter_rejects_unknown_value(
    organization_owner_client: Client, organization: Organization
) -> None:
    url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
    assert organization_owner_client.get(url, {"payment_method": "cheque"}).status_code == 422


def test_payment_due_by_only_for_unpaid_offline_pending(
    organization_owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    offline = _plan(tier, SubscriptionPaymentMethod.OFFLINE)
    free = _plan(tier, SubscriptionPaymentMethod.FREE)
    pending_user, active_user, free_user = _users(3)
    pending = _sub(organization, offline, pending_user)
    _sub(organization, offline, active_user, Sub.ACTIVE)
    _sub(organization, free, free_user, Sub.ACTIVE)

    rows = {r["user_id"]: r for r in _list(organization_owner_client, organization)}

    expected = pending.created_at + timedelta(days=APPLICATION_PAYMENT_WINDOW_DAYS)
    _assert_same_instant(rows[str(pending_user.id)]["payment_due_by"], expected)
    assert rows[str(active_user.id)]["payment_due_by"] is None
    assert rows[str(free_user.id)]["payment_due_by"] is None


def test_payment_due_by_moves_with_last_payment_activity(
    organization_owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    offline = _plan(tier, SubscriptionPaymentMethod.OFFLINE)
    (user,) = _users(1)
    sub = _sub(organization, offline, user)
    payment = MembershipPayment.objects.create(
        subscription=sub,
        amount=Decimal("10.00"),
        currency="EUR",
        status=MembershipPayment.PaymentStatus.VOIDED,
        period_start=timezone.now(),
        period_end=timezone.now() + timedelta(days=30),
    )

    (row,) = _list(organization_owner_client, organization)
    detail = organization_owner_client.get(
        reverse("api:get_subscription", kwargs={"slug": organization.slug, "sub_id": sub.id})
    ).json()

    expected = payment.updated_at + timedelta(days=APPLICATION_PAYMENT_WINDOW_DAYS)
    _assert_same_instant(row["payment_due_by"], expected)
    _assert_same_instant(detail["payment_due_by"], expected)  # the unannotated fallback agrees with the list


def test_list_query_count_does_not_grow_with_pending_offline_rows(
    organization_owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    django_assert_max_num_queries: t.Any,
) -> None:
    offline = _plan(tier, SubscriptionPaymentMethod.OFFLINE)
    users = _users(5)

    def _join(user: RevelUser) -> None:
        # A real offline joiner is held as AWAITING_PAYMENT (a row without a member costs the list one lookup each).
        OrganizationMember.objects.create(
            organization=organization, user=user, tier=tier, status=OrganizationMember.MembershipStatus.AWAITING_PAYMENT
        )
        _sub(organization, offline, user)

    _join(users[0])
    with CaptureQueriesContext(connection) as one_row:
        _list(organization_owner_client, organization)

    for user in users[1:]:
        _join(user)
    with django_assert_max_num_queries(len(one_row)):
        assert len(_list(organization_owner_client, organization)) == 5
