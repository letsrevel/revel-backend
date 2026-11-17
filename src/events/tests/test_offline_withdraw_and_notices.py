"""Offline join: member withdraw, staff notice on direct joins, and the never-paid cancel notification."""

import typing as t
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import translation
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
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import notifications as subscription_notifications
from events.service.subscription.offline import open_offline_subscription
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template
from notifications.signals import notification_requested

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus
Sub = MembershipSubscription.SubscriptionStatus
Omr = OrganizationMembershipRequest.Status


def _client(user: RevelUser) -> Client:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"  # type: ignore[attr-defined]
    return client


@pytest.fixture
def sent() -> t.Iterator[list[dict[str, t.Any]]]:
    calls: list[dict[str, t.Any]] = []
    with mock.patch.object(
        notification_requested,
        "send",
        side_effect=lambda *a, **kw: calls.append(
            {"type": kw["notification_type"], "context": kw.get("context", {}), "user": kw.get("user")}
        ),
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
    return MembershipTier.objects.create(organization=org, name="Open")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit="year",
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )


def _subscribe(user: RevelUser, org: Organization, plan: MembershipSubscriptionPlan) -> t.Any:
    return _client(user).post(
        reverse("api:subscribe_to_membership_plan", kwargs={"org_id": org.id}),
        data={"plan_id": str(plan.id)},
        content_type="application/json",
    )


def _withdraw(user: RevelUser, org: Organization, payload: dict[str, t.Any]) -> t.Any:
    return _client(user).post(
        reverse("api:cancel_my_membership_subscription", kwargs={"org_id": org.id}),
        data=payload,
        content_type="application/json",
    )


# ---- 1. member withdraw -------------------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"immediate": False}, {"immediate": True}])
def test_withdraw_never_paid_direct_join_then_rejoin(
    org: Organization, plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, payload: dict[str, t.Any]
) -> None:
    """Cancelling a never-paid offline sub is immediate whatever ``immediate`` says, and frees the person to rejoin."""
    first = _subscribe(nonmember_user, org, plan)
    assert first.status_code == 201, first.content
    first_id = first.json()["subscription"]["id"]

    response = _withdraw(nonmember_user, org, payload)

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancel_at_period_end"] is False
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert member.status == S.CANCELLED

    again = _subscribe(nonmember_user, org, plan)
    assert again.status_code == 201, again.content
    assert again.json()["subscription"]["id"] != first_id
    assert again.json()["subscription"]["status"] == "pending"
    member.refresh_from_db()
    assert member.status == S.AWAITING_PAYMENT


def test_withdraw_closes_application_and_allows_rejoin(
    org: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan, nonmember_user: RevelUser
) -> None:
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=tier, plan=plan, status=Omr.PENDING
    )
    sub = open_offline_subscription(plan, nonmember_user, application=application)
    application.refresh_from_db()
    assert application.status == Omr.APPROVED

    response = _withdraw(nonmember_user, org, {})

    assert response.status_code == 200, response.content
    sub.refresh_from_db()
    application.refresh_from_db()
    assert (sub.status, application.status) == (Sub.CANCELLED, Omr.CANCELLED)
    assert OrganizationMember.objects.get(organization=org, user=nonmember_user).status == S.CANCELLED

    again = _subscribe(nonmember_user, org, plan)
    assert again.status_code == 201, again.content
    assert again.json()["subscription"]["id"] != str(sub.id)
    assert again.json()["subscription"]["status"] == "pending"


def test_withdraw_without_a_live_subscription_is_404(org: Organization, nonmember_user: RevelUser) -> None:
    assert _withdraw(nonmember_user, org, {}).status_code == 404


# ---- 2. staff notice on direct joins ------------------------------------------------------------------------


def _joined(sent: list[dict[str, t.Any]]) -> list[dict[str, t.Any]]:
    return [c for c in sent if c["type"] == NotificationType.MEMBERSHIP_OFFLINE_JOINED]


def test_direct_join_notifies_staff_once(
    org: Organization,
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        assert _subscribe(nonmember_user, org, plan).status_code == 201
        assert _subscribe(nonmember_user, org, plan).status_code == 201  # idempotent repeat

    (notice,) = _joined(sent)
    assert notice["user"] == organization_owner_user
    ctx = notice["context"]
    assert (ctx["member_name"], ctx["tier_name"], ctx["plan_name"]) == (
        nonmember_user.display_name,
        "Open",
        "Annual",
    )
    assert ctx["frontend_url"].endswith(f"/org/{org.slug}/admin/members?tab=subscriptions&status=pending")


def test_application_flow_and_admin_create_do_not_notify_staff(
    org: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=tier, plan=plan, status=Omr.PENDING
    )
    with django_capture_on_commit_callbacks(execute=True):
        open_offline_subscription(plan, nonmember_user, application=application)  # approval flow
    assert _joined(sent) == []

    other = RevelUser.objects.create_user(username="other@example.com", email="other@example.com", password="x")
    with django_capture_on_commit_callbacks(execute=True):
        open_offline_subscription(plan, other)  # staff-initiated (admin create): no member_facing flag
    assert _joined(sent) == []


def test_staff_who_disabled_the_type_are_skipped(
    org: Organization,
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    prefs = organization_owner_user.notification_preferences
    prefs.notification_type_settings[NotificationType.MEMBERSHIP_OFFLINE_JOINED] = {"enabled": False}
    prefs.save()

    with django_capture_on_commit_callbacks(execute=True):
        assert _subscribe(nonmember_user, org, plan).status_code == 201

    assert _joined(sent) == []


@pytest.mark.django_db
def test_offline_joined_renders_in_every_channel(
    org: Organization, plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    sub = MembershipSubscription.objects.create(user=nonmember_user, organization=org, plan=plan, status=Sub.PENDING)
    with patch("notifications.tasks.dispatch_notification.delay"):
        subscription_notifications.dispatch_membership_offline_joined(sub)
    notification = Notification.objects.get(
        user=organization_owner_user, notification_type=NotificationType.MEMBERSHIP_OFFLINE_JOINED
    )
    template = get_template(notification.notification_type)
    with translation.override("en"):
        bodies = {
            "html": template.get_email_html_body(notification) or "",
            "txt": template.get_email_text_body(notification),
            "in_app": template.get_in_app_body(notification),
            "telegram": template.get_telegram_body(notification),
        }
        title = template.get_in_app_title(notification)
        subject = template.get_email_subject(notification)
    assert "awaiting payment" in title
    assert org.name in subject
    for channel, body in bodies.items():
        assert nonmember_user.display_name in body, channel
        assert "tab=subscriptions" in body.replace("&amp;", "&"), channel
        assert "awaiting payment" in body, channel


# ---- 4. never-paid cancel notification ----------------------------------------------------------------------


def _cancellations(sent: list[dict[str, t.Any]]) -> list[dict[str, t.Any]]:
    return [c for c in sent if c["type"] == NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED]


def test_never_paid_cancel_sends_request_cancelled_variant(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub = open_offline_subscription(plan, nonmember_user)
    sent.clear()

    subscription_lifecycle.cancel_subscription(sub)

    (cancelled,) = _cancellations(sent)
    assert cancelled["context"]["request_cancelled"] is True


def test_paid_cancel_keeps_the_regular_notification(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub = open_offline_subscription(plan, nonmember_user)
    subscription_lifecycle.record_payment(sub, amount=Decimal("50.00"), currency="EUR", recorded_by=None)
    sub.refresh_from_db()
    sent.clear()

    subscription_lifecycle.cancel_subscription(sub, immediate=True)

    (cancelled,) = _cancellations(sent)
    assert cancelled["context"]["request_cancelled"] is False


@pytest.mark.parametrize("request_cancelled", [True, False])
def test_cancellation_renders_branch_in_every_channel(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, request_cancelled: bool
) -> None:
    sub = MembershipSubscription.objects.create(
        user=nonmember_user, organization=plan.tier.organization, plan=plan, status=Sub.CANCELLED
    )
    with (
        patch("notifications.tasks.dispatch_notification.delay"),
        patch(
            "events.service.subscription.notifications.is_never_paid_on_offline_plan", return_value=request_cancelled
        ),
    ):
        subscription_notifications._dispatch_cancellation_confirmed(sub, immediate=True)  # noqa: SLF001
    notification = Notification.objects.get(
        user=nonmember_user, notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED
    )
    template = get_template(notification.notification_type)
    with translation.override("en"):
        bodies = {
            "html": template.get_email_html_body(notification) or "",
            "txt": template.get_email_text_body(notification),
            "in_app": template.get_in_app_body(notification),
            "telegram": template.get_telegram_body(notification),
        }
        subject = template.get_email_subject(notification)
        title = template.get_in_app_title(notification)
    assert ("request was cancelled" in subject) is request_cancelled
    assert ("request cancelled" in title) is request_cancelled
    for channel, body in bodies.items():
        assert ("membership request" in body) is request_cancelled, channel
        if channel in ("html", "txt"):
            assert ("Access ends" in body) is (not request_cancelled), channel
