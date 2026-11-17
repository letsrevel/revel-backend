"""Closing never-paid OFFLINE subscriptions: cascade on cancel/loss, pause guard, void revert and the 30-day sweep."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from accounts.tasks import gdpr as gdpr_tasks
from events.models import (
    MembershipPayment,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service import blacklist_service
from events.service.membership_manager import service as membership_manager_service
from events.service.organization_service import membership as membership_service
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import payment_edits, refunds
from events.service.subscription import plans as subscription_plans
from events.service.subscription.offline import open_offline_subscription
from events.tasks.subscriptions import APPLICATION_PAYMENT_WINDOW_DAYS, expire_unpaid_offline_subscriptions
from notifications.enums import NotificationType
from notifications.signals import notification_requested

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


@pytest.fixture
def sent() -> t.Iterator[list[dict[str, t.Any]]]:
    calls: list[dict[str, t.Any]] = []
    with mock.patch.object(
        notification_requested,
        "send",
        side_effect=lambda *a, **kw: calls.append({"type": kw["notification_type"], "context": kw.get("context", {})}),
    ):
        yield calls


def _join(
    plan: MembershipSubscriptionPlan, user: RevelUser, *, with_application: bool = True
) -> tuple[MembershipSubscription, OrganizationMembershipRequest | None]:
    application = None
    if with_application:
        application = OrganizationMembershipRequest.objects.create(
            user=user,
            organization=plan.tier.organization,
            tier=plan.tier,
            plan=plan,
            status=Omr.PENDING,
        )
    sub = open_offline_subscription(plan, user, application=application)
    return sub, application


def _member(sub: MembershipSubscription) -> OrganizationMember | None:
    return OrganizationMember.objects.filter(organization=sub.organization, user=sub.user).first()


def _pay(sub: MembershipSubscription) -> MembershipPayment:
    return subscription_lifecycle.record_payment(sub, amount=Decimal("50.00"), currency="EUR", recorded_by=None)


def _age(sub: MembershipSubscription, days: int) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(created_at=timezone.now() - timedelta(days=days))


def _staff_cancel(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    subscription_lifecycle.cancel_subscription(sub)


def _blacklist(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    blacklist_service.apply_blacklist_consequences(user, org)


def _gdpr(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    gdpr_tasks._cancel_live_subscriptions(user)  # noqa: SLF001


def _member_cancel_application(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    membership_manager_service.cancel_application(sub.originating_application.get())


def _member_cancel_subscription(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"  # type: ignore[attr-defined]
    response = client.post(
        reverse("api:cancel_my_membership_subscription", kwargs={"org_id": org.id}),
        data={"immediate": True},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _remove_member(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    membership_service.remove_member(org, user)


def _update_member_cancelled(sub: MembershipSubscription, user: RevelUser, org: Organization) -> None:
    membership_service.update_member(_member(sub), status=S.CANCELLED)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "close, expected_member, can_rejoin",
    [
        pytest.param(_staff_cancel, S.CANCELLED, True, id="staff-cancel"),
        pytest.param(_blacklist, S.BANNED, False, id="blacklist"),
        pytest.param(_gdpr, S.CANCELLED, True, id="gdpr"),
        pytest.param(_member_cancel_application, S.CANCELLED, True, id="member-cancel-application"),
        pytest.param(_member_cancel_subscription, S.CANCELLED, True, id="member-cancel-subscription"),
        pytest.param(_remove_member, None, True, id="remove-member"),
        pytest.param(_update_member_cancelled, S.CANCELLED, True, id="update-member-cancelled"),
    ],
)
def test_close_paths_cascade(
    close: t.Callable[[MembershipSubscription, RevelUser, Organization], None],
    expected_member: OrganizationMember.MembershipStatus | None,
    can_rejoin: bool,
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
) -> None:
    """Closing a never-paid joiner takes the application and the held member with it; they can join again."""
    sub, application = _join(plan, nonmember_user)
    assert application is not None
    assert _member(sub).status == S.AWAITING_PAYMENT  # type: ignore[union-attr]

    close(sub, nonmember_user, organization)

    sub.refresh_from_db()
    application.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    assert application.status == Omr.CANCELLED
    member = _member(sub)
    assert (member.status if member else None) == expected_member
    if can_rejoin:
        again = open_offline_subscription(plan, nonmember_user)
        assert again.pk != sub.pk
        assert _member(again).status == S.AWAITING_PAYMENT  # type: ignore[union-attr]


def test_member_cancel_application_cancels_linked_subscription(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub, application = _join(plan, nonmember_user)
    assert application is not None
    application.refresh_from_db()
    assert application.status == Omr.APPROVED  # linking approved it

    membership_manager_service.cancel_application(application)

    sub.refresh_from_db()
    assert sub.status == Sub.CANCELLED


def test_close_legacy_active_member_on_cancel(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    """A pre-change row (ACTIVE member at the plan tier + never-paid PENDING sub) is closed with the sub."""
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=plan.tier)
    sub = MembershipSubscription.objects.create(
        user=nonmember_user, organization=organization, plan=plan, status=Sub.PENDING
    )

    subscription_lifecycle.cancel_subscription(sub)

    assert _member(sub).status == S.CANCELLED  # type: ignore[union-attr]


def test_close_keeps_upgrading_free_member(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    """An ACTIVE member on another tier keeps their free membership when the paid join is cancelled."""
    free = MembershipTier.objects.get(organization=organization, name="General membership")
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=free)
    sub = MembershipSubscription.objects.create(
        user=nonmember_user, organization=organization, plan=plan, status=Sub.PENDING
    )

    subscription_lifecycle.cancel_subscription(sub)

    member = _member(sub)
    assert (member.status, member.tier_id) == (S.ACTIVE, free.pk)  # type: ignore[union-attr]


def test_paid_subscription_cancel_does_not_touch_application(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub, application = _join(plan, nonmember_user)
    assert application is not None
    _pay(sub)
    sub.refresh_from_db()

    subscription_lifecycle.cancel_subscription(sub, immediate=True)

    application.refresh_from_db()
    assert application.status == Omr.COMPLETED


def test_pause_never_paid_refused(plan: MembershipSubscriptionPlan, nonmember_user: RevelUser) -> None:
    sub, _ = _join(plan, nonmember_user, with_application=False)

    with pytest.raises(HttpError) as exc:
        subscription_lifecycle.pause_subscription(sub)

    assert exc.value.status_code == 400
    assert "awaiting its first payment" in str(exc.value.message)
    sub.refresh_from_db()
    assert sub.status == Sub.PENDING


def test_void_only_payment_reverts_to_awaiting(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub, _ = _join(plan, nonmember_user, with_application=False)
    payment = _pay(sub)
    assert _member(sub).status == S.ACTIVE  # type: ignore[union-attr]
    assert MembershipRecord.objects.filter(user=nonmember_user, valid_until__isnull=True).exists()

    payment_edits.void_payment(payment, recorded_by=None)

    sub.refresh_from_db()
    assert sub.status == Sub.PENDING
    assert _member(sub).status == S.AWAITING_PAYMENT  # type: ignore[union-attr]
    record = MembershipRecord.objects.get(user=nonmember_user)
    assert record.valid_until is not None
    assert record.ended_reason == MembershipRecord.EndedReason.CANCELLED  # never EXPELLED


def test_void_with_other_payment_keeps_member_active(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    sub, _ = _join(plan, nonmember_user, with_application=False)
    _pay(sub)
    sub.refresh_from_db()
    second = _pay(sub)

    payment_edits.void_payment(second, recorded_by=None)

    assert _member(sub).status == S.ACTIVE  # type: ignore[union-attr]


def test_update_member_rejects_awaiting_payment(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, member_user: RevelUser, organization: Organization
) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user, status=S.ACTIVE)

    with pytest.raises(HttpError) as exc:
        membership_service.update_member(member, status=S.AWAITING_PAYMENT)

    assert exc.value.status_code == 400
    assert str(exc.value.message) == "This status is set by the system."


def test_pausing_member_skips_never_paid_subscription(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=plan.tier)
    sub = MembershipSubscription.objects.create(
        user=nonmember_user, organization=organization, plan=plan, status=Sub.PENDING
    )

    membership_service.update_member(_member(sub), status=S.PAUSED)  # type: ignore[arg-type]

    sub.refresh_from_db()
    assert sub.status == Sub.PENDING
    assert _member(sub).status == S.PAUSED  # type: ignore[union-attr]


def test_sweep_cancels_after_30_days_and_notifies(
    plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    sub, application = _join(plan, nonmember_user)
    assert application is not None
    _age(sub, APPLICATION_PAYMENT_WINDOW_DAYS + 1)
    sent.clear()

    with django_capture_on_commit_callbacks(execute=True):
        assert expire_unpaid_offline_subscriptions() == 1

    sub.refresh_from_db()
    application.refresh_from_db()
    assert (sub.status, application.status) == (Sub.CANCELLED, Omr.CANCELLED)
    assert _member(sub).status == S.CANCELLED  # type: ignore[union-attr]
    expired = [c for c in sent if c["type"] == NotificationType.SUBSCRIPTION_EXPIRED]
    assert len(expired) == 1
    assert expired[0]["context"]["lapsed"] is True
    assert NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED not in [c["type"] for c in sent]


def test_sweep_includes_upgrading_free_member(
    plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> None:
    free = MembershipTier.objects.get(organization=organization, name="General membership")
    OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=S.ACTIVE, tier=free)
    sub = MembershipSubscription.objects.create(
        user=nonmember_user, organization=organization, plan=plan, status=Sub.PENDING
    )
    _age(sub, APPLICATION_PAYMENT_WINDOW_DAYS + 1)

    assert expire_unpaid_offline_subscriptions() == 1

    assert _member(sub).status == S.ACTIVE  # type: ignore[union-attr]


def test_sweep_skips_paid_and_legacy_active(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    nonmember_user: RevelUser,
    member_user: RevelUser,
    django_user_model: t.Type[RevelUser],
) -> None:
    # paid: ACTIVE subscription
    paid, _ = _join(plan, nonmember_user, with_application=False)
    _pay(paid)
    _age(paid, 90)
    # legacy: ACTIVE member at the plan tier + never-paid PENDING row
    OrganizationMember.objects.create(organization=organization, user=member_user, status=S.ACTIVE, tier=plan.tier)
    legacy = MembershipSubscription.objects.create(
        user=member_user, organization=organization, plan=plan, status=Sub.PENDING
    )
    _age(legacy, 90)
    # fresh awaiting row (inside the window)
    fresh_user = django_user_model.objects.create_user(username="fresh", email="fresh@example.com", password="pw")
    fresh, _ = _join(plan, fresh_user, with_application=False)

    assert expire_unpaid_offline_subscriptions() == 0

    for sub, status in ((paid, Sub.ACTIVE), (legacy, Sub.PENDING), (fresh, Sub.PENDING)):
        sub.refresh_from_db()
        assert sub.status == status


def test_refunding_only_offline_payment_cancels_member(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    """Money moved then came back: the paid member leaves with the refunded subscription (not stuck ACTIVE)."""
    sub, _ = _join(plan, nonmember_user, with_application=False)
    payment = _pay(sub)
    assert _member(sub).status == S.ACTIVE  # type: ignore[union-attr]

    refunds.refund_payment(payment, recorded_by=None)

    sub.refresh_from_db()
    assert sub.status == Sub.CANCELLED
    assert _member(sub).status == S.CANCELLED  # type: ignore[union-attr]


def test_direct_terminal_save_closes_held_member_and_application(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, sent: list[dict[str, t.Any]]
) -> None:
    """Any path that saves the row terminal (Django admin, a future flow) closes it, via the signal."""
    sub, application = _join(plan, nonmember_user)
    assert application is not None

    sub.status = Sub.CANCELLED
    sub.save()

    application.refresh_from_db()
    assert application.status == Omr.CANCELLED
    assert _member(sub).status == S.CANCELLED  # type: ignore[union-attr]
