"""Code-review fixes on the offline-paid membership flow (PR #1396)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)
from events.service.membership_manager import MembershipApplicationIneligibleError
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import offline
from events.service.subscription.eligibility import subscribe_to_plan
from events.service.subscription.offline import open_offline_subscription
from events.utils.subscription_payment import APPLICATION_PAYMENT_WINDOW_DAYS
from notifications.enums import MANDATORY_TYPES, TRANSACTIONAL_TYPES, NotificationType
from notifications.signals import notification_requested

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus
Sub = MembershipSubscription.SubscriptionStatus


@pytest.fixture
def sent() -> t.Iterator[list[dict[str, t.Any]]]:
    calls: list[dict[str, t.Any]] = []
    with mock.patch.object(
        notification_requested,
        "send",
        side_effect=lambda *a, **kw: calls.append({"type": kw["notification_type"], "context": kw.get("context", {})}),
    ):
        yield calls


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save()
    return organization


@pytest.fixture
def tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Gold")


def _offline_plan(tier: MembershipTier, name: str = "Annual") -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=name,
        price=Decimal("50.00"),
        currency="EUR",
        period_unit="year",
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )


def _client(user: RevelUser) -> Client:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"  # type: ignore[attr-defined]
    return client


def _types(sent: list[dict[str, t.Any]]) -> list[NotificationType]:
    return [c["type"] for c in sent]


def test_payment_requested_is_mandatory_and_immediate() -> None:
    """The only message carrying the payment instructions can be neither digested nor opted out of."""
    assert NotificationType.MEMBERSHIP_PAYMENT_REQUESTED in MANDATORY_TYPES
    assert NotificationType.MEMBERSHIP_PAYMENT_REQUESTED in TRANSACTIONAL_TYPES


def test_resaving_old_lapsed_subscription_does_not_cancel_hand_added_member(
    tier: MembershipTier, org: Organization, nonmember_user: RevelUser
) -> None:
    """close_unpaid_offline runs on the transition to terminal only, not on every later save of the row."""
    plan = _offline_plan(tier)
    sub = open_offline_subscription(plan, nonmember_user)
    subscription_lifecycle.cancel_subscription(sub, immediate=True)
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert member.status == S.CANCELLED
    # Staff later add the person by hand.
    member.status = S.ACTIVE
    member.tier = tier
    member.save()

    sub.refresh_from_db()
    sub.save()  # e.g. an admin re-saves the historical row

    member.refresh_from_db()
    assert member.status == S.ACTIVE


def test_cancel_runs_close_once(tier: MembershipTier, nonmember_user: RevelUser) -> None:
    """The post_save signal is the single closer on the transition (no second explicit call)."""
    plan = _offline_plan(tier)
    sub = open_offline_subscription(plan, nonmember_user)
    with mock.patch.object(offline, "close_unpaid_offline", wraps=offline.close_unpaid_offline) as spy:
        subscription_lifecycle.cancel_subscription(sub, immediate=True)
    assert spy.call_count == 1


def test_first_payment_receipt_for_upgrading_member_has_new_period_end(
    tier: MembershipTier,
    org: Organization,
    member_user: RevelUser,
    sent: list[dict[str, t.Any]],
) -> None:
    plan = _offline_plan(tier)
    OrganizationMember.objects.create(
        organization=org,
        user=member_user,
        status=S.ACTIVE,
        tier=MembershipTier.objects.get(organization=org, name="General membership"),
    )
    sub = open_offline_subscription(plan, member_user)
    sent.clear()

    subscription_lifecycle.record_payment(sub, amount=Decimal("50.00"), currency="EUR", recorded_by=None)

    sub.refresh_from_db()
    assert sub.current_period_end is not None
    receipts = [c for c in sent if c["type"] == NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED]
    assert len(receipts) == 1
    assert receipts[0]["context"]["period_end"] == sub.current_period_end.date().isoformat()


def test_self_serve_apply_is_not_an_acceptance_and_no_staff_notice(
    tier: MembershipTier,
    org: Organization,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(tier)

    with django_capture_on_commit_callbacks(execute=True):
        response = _client(nonmember_user).post(
            reverse("api:apply_for_membership", kwargs={"slug": org.slug}),
            data={"tier_id": str(tier.id), "plan_id": str(plan.id)},
            content_type="application/json",
        )

    assert response.status_code == 201, response.content
    # Staff hear about the application itself, never a second "joined offline" notice.
    assert NotificationType.MEMBERSHIP_OFFLINE_JOINED not in _types(sent)
    requested = next(c for c in sent if c["type"] == NotificationType.MEMBERSHIP_PAYMENT_REQUESTED)
    assert requested["context"]["application_accepted"] is False
    assert requested["context"]["payment_window_days"] == APPLICATION_PAYMENT_WINDOW_DAYS


def test_staff_approval_still_leads_with_acceptance(
    tier: MembershipTier,
    org: Organization,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(tier)
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=tier, plan=plan
    )
    with django_capture_on_commit_callbacks(execute=True):
        open_offline_subscription(plan, nonmember_user, application=application)
    assert sent[0]["context"]["application_accepted"] is True


def test_subscribe_to_second_offline_plan_is_structured_duplicate(
    tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    first = _offline_plan(tier, "Annual")
    second = _offline_plan(tier, "Other")
    subscribe_to_plan(first, nonmember_user)

    with pytest.raises(MembershipApplicationIneligibleError) as exc:
        subscribe_to_plan(second, nonmember_user)

    assert exc.value.eligibility.reason_code == "duplicate_active_subscription"


def test_subscribe_again_to_same_offline_plan_still_idempotent(tier: MembershipTier, nonmember_user: RevelUser) -> None:
    plan = _offline_plan(tier)
    first, _ = subscribe_to_plan(plan, nonmember_user)
    second, _ = subscribe_to_plan(plan, nonmember_user)
    assert first.pk == second.pk


def test_create_staff_subscription_requests_payment_once_and_is_idempotent(
    tier: MembershipTier,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(tier)

    with django_capture_on_commit_callbacks(execute=True):
        first = subscription_lifecycle.create_staff_subscription(plan, nonmember_user)
        second = subscription_lifecycle.create_staff_subscription(plan, nonmember_user)

    assert first.pk == second.pk
    assert _types(sent) == [NotificationType.MEMBERSHIP_PAYMENT_REQUESTED]
    assert OrganizationMember.objects.get(user=nonmember_user, organization=plan.tier.organization).status == (
        S.AWAITING_PAYMENT
    )


def test_create_staff_subscription_with_initial_payment_activates(
    tier: MembershipTier, nonmember_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    from events.service.subscription.core import InitialPayment

    plan = _offline_plan(tier)
    sub = subscription_lifecycle.create_staff_subscription(
        plan,
        nonmember_user,
        initial_payment=InitialPayment(
            amount=Decimal("50.00"),
            currency="EUR",
            recorded_by=organization_owner_user,
            occurred_at=timezone.now() - timedelta(minutes=1),
        ),
    )
    assert sub.status == Sub.ACTIVE
