"""Staff ``update_member`` on a member awaiting their first offline payment (#1399)."""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service.organization_service import membership as membership_service
from events.service.subscription import plans as subscription_plans
from events.service.subscription.offline import open_offline_subscription
from notifications.enums import NotificationType
from notifications.signals import notification_requested

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus
Sub = MembershipSubscription.SubscriptionStatus
Omr = OrganizationMembershipRequest.Status

PAUSE_MESSAGE = "A member awaiting their first payment can't be paused. Cancel their request instead."
ACTIVATE_MESSAGE = "Record their first payment to activate this member."


@pytest.fixture
def plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    return subscription_plans.create_plan(
        tier, name="Annual", price=Decimal("50.00"), currency="EUR", period_unit="year", period_count=1
    )


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
def awaiting(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember]:
    organization = plan.tier.organization
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=organization, tier=plan.tier, plan=plan, status=Omr.PENDING
    )
    sub = open_offline_subscription(plan, nonmember_user, application=application)
    member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
    assert member.status == S.AWAITING_PAYMENT
    return sub, application, member


def _cancellations(sent: list[dict[str, t.Any]]) -> list[dict[str, t.Any]]:
    return [c for c in sent if c["type"] == NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED]


@pytest.mark.parametrize(("status", "rejoinable"), [(S.CANCELLED, True), (S.BANNED, False)])
def test_cancel_or_ban_closes_the_unpaid_request(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
    status: S,
    rejoinable: bool,
) -> None:
    sub, application, member = awaiting
    sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        membership_service.update_member(member, status=status)

    sub.refresh_from_db()
    application.refresh_from_db()
    member.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    assert application.status == Omr.CANCELLED
    assert member.status == status
    (cancelled,) = _cancellations(sent)
    assert cancelled["context"]["request_cancelled"] is True
    assert cancelled["context"]["rejoinable"] is rejoinable


def test_cancelled_person_can_subscribe_again(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
) -> None:
    organization = plan.tier.organization
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()
    membership_service.update_member(awaiting[2], status=S.CANCELLED)

    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(nonmember_user).access_token}"  # type: ignore[attr-defined]
    response = client.post(
        reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id}),
        data={"plan_id": str(plan.id)},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content


def test_pause_is_refused_and_writes_nothing(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
) -> None:
    sub, application, member = awaiting
    sub.refresh_from_db()
    application.refresh_from_db()
    member.refresh_from_db()
    before = (sub.status, application.status, member.status)
    with pytest.raises(HttpError) as exc:
        membership_service.update_member(member, status=S.PAUSED)

    assert exc.value.status_code == 400
    assert exc.value.message == PAUSE_MESSAGE
    sub.refresh_from_db()
    application.refresh_from_db()
    member.refresh_from_db()
    assert (sub.status, application.status, member.status) == before
    assert member.status == S.AWAITING_PAYMENT and sub.status == Sub.PENDING


def test_pause_through_the_endpoint_returns_400(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
    organization_owner_client: Client,
) -> None:
    sub, _, member = awaiting
    url = reverse(
        "api:update_organization_member", kwargs={"slug": member.organization.slug, "user_id": member.user_id}
    )
    response = organization_owner_client.put(url, data={"status": "paused"}, content_type="application/json")

    assert response.status_code == 400, response.content
    assert response.json()["detail"] == PAUSE_MESSAGE
    sub.refresh_from_db()
    assert sub.status == Sub.PENDING


def test_activate_is_refused_and_writes_nothing(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
    organization_owner_client: Client,
) -> None:
    sub, application, member = awaiting
    url = reverse(
        "api:update_organization_member", kwargs={"slug": member.organization.slug, "user_id": member.user_id}
    )
    response = organization_owner_client.put(url, data={"status": "active"}, content_type="application/json")

    assert response.status_code == 400, response.content
    assert response.json()["detail"] == ACTIVATE_MESSAGE
    sub.refresh_from_db()
    application.refresh_from_db()
    member.refresh_from_db()
    assert (sub.status, application.status, member.status) == (Sub.PENDING, Omr.APPROVED, S.AWAITING_PAYMENT)


def test_stale_instance_is_rechecked_under_the_lock(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
) -> None:
    """A member instance loaded as ACTIVE but meanwhile moved to AWAITING_PAYMENT is judged on the DB row."""
    _, _, member = awaiting
    stale = OrganizationMember.objects.get(pk=member.pk)
    stale.status = S.ACTIVE  # stale in-memory view; the row says AWAITING_PAYMENT
    with pytest.raises(HttpError) as exc:
        membership_service.update_member(stale, status=S.PAUSED)
    assert exc.value.message == PAUSE_MESSAGE


def test_ban_through_the_endpoint_closes_the_request(
    awaiting: tuple[MembershipSubscription, OrganizationMembershipRequest, OrganizationMember],
    organization_owner_client: Client,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    sub, application, member = awaiting
    sent.clear()
    url = reverse(
        "api:update_organization_member", kwargs={"slug": member.organization.slug, "user_id": member.user_id}
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = organization_owner_client.put(url, data={"status": "banned"}, content_type="application/json")

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "banned"
    sub.refresh_from_db()
    application.refresh_from_db()
    member.refresh_from_db()
    assert (sub.status, application.status) == (Sub.CANCELLED, Omr.CANCELLED)
    assert member.status == S.BANNED
    (cancelled,) = _cancellations(sent)
    assert cancelled["context"]["rejoinable"] is False
