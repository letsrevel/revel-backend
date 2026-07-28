"""Ban / removal must stop billing (membership-loss subscription cancellation).

Banning or removing a member used to leave their subscription untouched: Stripe
kept charging a banned member, and a *removed* member was silently re-created as
ACTIVE by the next ``invoice.paid``. These tests pin the three coherence points
that now terminalize the subscription — blacklist, ``update_member`` → BANNED,
``remove_member`` — plus the webhook-side defense-in-depth that refuses to
re-mint a hard-blacklisted member.
"""

import time
import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import blacklist_service, organization_service, subscription_stripe_sync

pytestmark = pytest.mark.django_db

_ACTIVE = MembershipSubscription.SubscriptionStatus.ACTIVE
_CANCELLED = MembershipSubscription.SubscriptionStatus.CANCELLED
_BANNED = OrganizationMember.MembershipStatus.BANNED
_MEMBER_ACTIVE = OrganizationMember.MembershipStatus.ACTIVE


@pytest.fixture
def online_plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def offline_plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Offline",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


def _invoice(stripe_sub_id: str, *, invoice_id: str) -> dict[str, t.Any]:
    now = int(time.time())
    return {
        "id": invoice_id,
        "status": "paid",
        "subscription": stripe_sub_id,
        "amount_paid": 1000,
        "currency": "eur",
        "payment_intent": "pi_membership_loss",
        "billing_reason": "subscription_cycle",
        "lines": {"data": [{"period": {"start": now - 86400, "end": now + 30 * 86400}}]},
    }


class TestBlacklistCancelsSubscription:
    """``apply_blacklist_consequences`` terminalizes the member's subscription."""

    def test_online_subscription_is_cancelled_and_stripe_dispatched(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_live",
            cancel_at_period_end=True,
        )

        with patch("events.service.subscription_stripe_service.cancel_stripe_subscription_best_effort") as cancel:
            with django_capture_on_commit_callbacks(execute=True):
                blacklist_service.apply_blacklist_consequences(member_user, organization)

        sub.refresh_from_db()
        assert sub.status == _CANCELLED
        assert sub.cancelled_at is not None
        assert sub.cancel_at_period_end is False
        cancel.assert_called_once()
        member = OrganizationMember.objects.get(organization=organization, user=member_user)
        assert member.status == _BANNED

    def test_offline_subscription_is_cancelled_without_stripe_dispatch(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=offline_plan,
            organization=organization,
            status=_ACTIVE,
        )

        with patch("events.service.subscription_stripe_service.cancel_stripe_subscription_best_effort") as cancel:
            with django_capture_on_commit_callbacks(execute=True):
                blacklist_service.apply_blacklist_consequences(member_user, organization)

        sub.refresh_from_db()
        assert sub.status == _CANCELLED
        # OFFLINE plans have no Stripe side to close.
        cancel.assert_not_called()

    def test_owner_is_not_banned_and_keeps_subscription(
        self,
        organization: Organization,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        owner = organization.owner
        sub = MembershipSubscription.objects.create(
            user=owner,
            plan=offline_plan,
            organization=organization,
            status=_ACTIVE,
        )

        blacklist_service.apply_blacklist_consequences(owner, organization)

        sub.refresh_from_db()
        assert sub.status == _ACTIVE


class TestMemberUpdateAndRemoveCancelSubscription:
    """``update_member`` → BANNED and ``remove_member`` terminalize the subscription."""

    def test_update_member_to_banned_cancels_subscription(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user, plan=offline_plan, organization=organization, status=_ACTIVE
        )

        organization_service.update_member(member, status=_BANNED)

        sub.refresh_from_db()
        assert sub.status == _CANCELLED

    def test_update_member_to_non_banned_status_leaves_subscription(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user, plan=offline_plan, organization=organization, status=_ACTIVE
        )

        organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        sub.refresh_from_db()
        assert sub.status == _ACTIVE

    def test_remove_member_cancels_subscription_before_delete(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user, plan=offline_plan, organization=organization, status=_ACTIVE
        )

        organization_service.remove_member(organization, member_user)

        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()
        sub.refresh_from_db()
        assert sub.status == _CANCELLED


class TestInvoicePaidForBlacklistedUser:
    """Defense-in-depth: a paid invoice must not re-mint a hard-blacklisted member."""

    def test_no_member_created_payment_recorded_incident_raised(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        # Hard-blacklist via an email-only entry (no user FK) so the post_save
        # signal does NOT pre-create a BANNED member / cancel the sub — this
        # reproduces the race where the payment lands before the ban settles.
        Blacklist.objects.create(organization=organization, email=member_user.email, created_by=organization.owner)
        MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_race",
        )
        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()

        with patch("events.service.stripe_incidents.record_subscription_paid_while_blacklisted") as incident:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_race", invoice_id="in_race"), succeeded=True
            )

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()
        incident.assert_called_once()

    def test_existing_banned_member_is_preserved(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=member_user, status=_BANNED)
        MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_banned",
        )

        with patch("events.service.stripe_incidents.record_subscription_paid_while_blacklisted") as incident:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_banned", invoice_id="in_banned"), succeeded=True
            )

        assert payment is not None
        member = OrganizationMember.objects.get(organization=organization, user=member_user)
        assert member.status == _BANNED
        incident.assert_not_called()

    def test_non_blacklisted_removed_member_is_recreated(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        # A plain removal (no blacklist) that later re-subscribes: minting the
        # member again is correct once removal cancels the old sub, so this path
        # must NOT be blocked.
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_resub",
        )
        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice("sub_resub", invoice_id="in_resub"), succeeded=True
        )

        sub.refresh_from_db()
        member = OrganizationMember.objects.get(organization=organization, user=member_user)
        assert member.status == _MEMBER_ACTIVE
