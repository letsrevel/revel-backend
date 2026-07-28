"""Ban / removal must stop billing (membership-loss subscription cancellation).

Banning or removing a member used to leave their subscription untouched: Stripe
kept charging a banned member, and a *removed* member was silently re-created as
ACTIVE by the next ``invoice.paid``. These tests pin the coherence points
that now stop the billing — blacklist, ``update_member`` → BANNED / CANCELLED /
PAUSED, ``remove_member`` — plus the webhook-side defense-in-depth that refuses
to re-mint a hard-blacklisted member.
"""

import time
import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe
from django.utils import timezone
from ninja.errors import HttpError

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
    """``update_member`` and ``remove_member`` mirror the staff decision onto the subscription."""

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

    def test_update_member_to_cancelled_cancels_subscription(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """CANCELLED is a revocation (``for_user`` treats it as no membership) — stop billing."""
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_staff_cancel",
        )

        with patch("events.service.subscription_stripe_service.cancel_stripe_subscription_best_effort") as cancel:
            with django_capture_on_commit_callbacks(execute=True):
                organization_service.update_member(member, status=OrganizationMember.MembershipStatus.CANCELLED)

        sub.refresh_from_db()
        assert sub.status == _CANCELLED
        assert sub.cancelled_at is not None
        cancel.assert_called_once()
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_update_member_to_paused_pauses_subscription(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A suspended member must not keep paying for access they no longer have."""
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user, plan=offline_plan, organization=organization, status=_ACTIVE
        )

        organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAUSED
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_update_member_to_paused_pauses_collection_on_stripe(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_staff_pause",
        )

        with patch("events.service.subscription_stripe_service.stripe.Subscription.modify") as modify:
            organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        modify.assert_called_once()
        assert modify.call_args.kwargs["pause_collection"] == {"behavior": "void"}
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAUSED

    def test_stripe_pause_failure_does_not_persist_the_paused_member(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A 502 from Stripe must leave the member un-paused.

        ninja catches ``HttpError`` *inside* the view, so under ATOMIC_REQUESTS
        the request transaction commits regardless: before ``update_member``
        became atomic the member was persisted PAUSED while Stripe kept
        collecting, and the caller was told 502.
        """
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_pause_boom",
        )

        with patch(
            "events.service.subscription_stripe_service.stripe.Subscription.modify",
            side_effect=stripe.error.APIConnectionError("boom"),
        ):
            with pytest.raises(HttpError) as exc:
                organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        assert exc.value.status_code == 502
        member.refresh_from_db()
        assert member.status == _MEMBER_ACTIVE
        sub.refresh_from_db()
        assert sub.status == _ACTIVE

    def test_paused_member_is_not_reactivated_by_the_next_renewal(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """The regression: every subscription save re-syncs the member status.

        Before the fix the subscription stayed ACTIVE, so the next renewal save
        flipped the staff-paused member back to ACTIVE.
        """
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=_ACTIVE,
            stripe_subscription_id="sub_renewal",
        )

        with patch("events.service.subscription_stripe_service.stripe.Subscription.modify"):
            organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        # Simulate the next billing-cycle write (renewals bump the period bounds).
        sub.refresh_from_db()
        sub.current_period_start = timezone.now()
        sub.current_period_end = timezone.now() + timedelta(days=30)
        sub.save(update_fields=["current_period_start", "current_period_end", "updated_at"])

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_update_member_to_paused_skips_subscription_scheduled_for_cancellation(
        self,
        organization: Organization,
        member_user: RevelUser,
        offline_plan: MembershipSubscriptionPlan,
    ) -> None:
        """``pause_subscription`` refuses these; billing already ends at the period boundary."""
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=offline_plan,
            organization=organization,
            status=_ACTIVE,
            cancel_at_period_end=True,
        )

        organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        sub.refresh_from_db()
        assert sub.status == _ACTIVE
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_update_member_to_paused_skips_online_subscription_without_stripe_link(
        self,
        organization: Organization,
        member_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """Nothing to pause on Stripe — checkout never completed, so nothing is billing."""
        member = OrganizationMember.objects.create(organization=organization, user=member_user, status=_MEMBER_ACTIVE)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )

        organization_service.update_member(member, status=OrganizationMember.MembershipStatus.PAUSED)

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

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
