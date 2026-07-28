"""Regression tests for the money-correctness fixes from the #774 review.

Each class pins one previously-broken invariant. They are grouped here rather
than scattered so the connection between them stays visible: every one is a path
where Stripe took money and the local state disagreed.
"""

import datetime
import time
import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service import subscription_stripe_sync
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


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
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="fixes_member", email="fixes-member@example.com", password="pass"
    )


def _invoice(
    stripe_sub_id: str,
    *,
    invoice_id: str,
    lines: list[dict[str, t.Any]] | None = None,
    billing_reason: str = "subscription_cycle",
) -> dict[str, t.Any]:
    now = int(time.time())
    return {
        "id": invoice_id,
        "status": "paid",
        "subscription": stripe_sub_id,
        "amount_paid": 1000,
        "currency": "eur",
        "payment_intent": "pi_fixes",
        "billing_reason": billing_reason,
        "lines": {"data": lines or [{"period": {"start": now - 86400, "end": now + 30 * 86400}}]},
    }


class TestTerminalRowNeverAdvances:
    """An ``invoice.paid`` against a CANCELLED/EXPIRED row must freeze the row.

    Reachable whenever the best-effort Stripe cancel that follows a local
    terminalization fails: Stripe keeps dunning and a Smart Retry eventually
    succeeds against a row we already closed.
    """

    @pytest.mark.parametrize(
        "terminal_status",
        [
            MembershipSubscription.SubscriptionStatus.CANCELLED,
            MembershipSubscription.SubscriptionStatus.EXPIRED,
        ],
    )
    def test_period_is_not_advanced_and_incident_is_raised(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        terminal_status: MembershipSubscription.SubscriptionStatus,
    ) -> None:
        original_end = timezone.now() - datetime.timedelta(days=5)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=terminal_status,
            stripe_subscription_id="sub_terminal",
            current_period_end=original_end,
        )

        with patch("events.service.stripe_incidents.record_subscription_paid_while_terminal") as incident:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                _invoice("sub_terminal", invoice_id="in_terminal"), succeeded=True
            )

        sub.refresh_from_db()
        # The money moved, so the ledger row is still written...
        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        # ...but the terminal row is untouched and an operator is alerted.
        assert sub.status == terminal_status
        assert sub.current_period_end == original_end
        assert not OrganizationMember.objects.filter(organization=sub.organization, user=member_user).exists()
        incident.assert_called_once()


class TestProrationDoesNotCorruptPeriodAnchor:
    """A proration-only invoice describes no billing period.

    Stripe sorts prorations first, so trusting ``lines.data[0]`` would rewrite
    ``current_period_start`` to "now" and break the exact-match test that
    ``_is_full_refund_of_current_period`` relies on to auto-cancel.
    """

    def test_proration_only_invoice_leaves_anchor_alone(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        now = int(time.time())
        anchor_start = datetime.datetime.fromtimestamp(now - 10 * 86400, tz=timezone.get_current_timezone())
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_proration",
            current_period_start=anchor_start,
        )

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice(
                "sub_proration",
                invoice_id="in_proration",
                lines=[{"proration": True, "period": {"start": now, "end": now + 86400}}],
                billing_reason="subscription_update",
            ),
            succeeded=True,
        )

        sub.refresh_from_db()
        assert sub.current_period_start == anchor_start

    def test_recurring_line_is_preferred_over_leading_proration(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        now = int(time.time())
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_mixed",
        )
        recurring_start, recurring_end = now + 86400, now + 31 * 86400

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice(
                "sub_mixed",
                invoice_id="in_mixed",
                lines=[
                    {"proration": True, "period": {"start": now, "end": now + 100}},
                    {
                        "proration": False,
                        "pricing": {"price_details": {"price": "price_test"}},
                        "period": {"start": recurring_start, "end": recurring_end},
                    },
                ],
            ),
            succeeded=True,
        )

        sub.refresh_from_db()
        assert sub.current_period_start is not None
        assert int(sub.current_period_start.timestamp()) == recurring_start
        assert sub.current_period_end is not None
        assert int(sub.current_period_end.timestamp()) == recurring_end


class TestStalePaidInvoiceDoesNotRewindThePeriod:
    """The billing anchor only ever moves forward on the success path.

    Stripe gives no delivery-order guarantee, and an *open* invoice from an
    earlier cycle can be paid (hosted invoice page, late dunning retry) after a
    later cycle already settled. Rewinding ``current_period_end`` into the past
    makes the grace-expiry beat flip a paid-up member to PAST_DUE, and corrupts
    the anchor ``_is_full_refund_of_current_period`` matches on.
    """

    @staticmethod
    def _dt(epoch: int) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)

    def test_older_invoice_does_not_rewind_an_active_row(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        now = int(time.time())
        current_start, current_end = self._dt(now - 5 * 86400), self._dt(now + 25 * 86400)
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_stale",
            current_period_start=current_start,
            current_period_end=current_end,
        )

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice(
                "sub_stale",
                invoice_id="in_stale",
                lines=[{"period": {"start": now - 35 * 86400, "end": now - 5 * 86400}}],
            ),
            succeeded=True,
        )

        sub.refresh_from_db()
        assert sub.current_period_start == current_start
        assert sub.current_period_end == current_end
        # The ledger row still records the period that invoice actually covered.
        payment = MembershipPayment.objects.get(stripe_invoice_id="in_stale")
        assert int(payment.period_end.timestamp()) == now - 5 * 86400

    def test_renewal_still_advances_the_period(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        now = int(time.time())
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_renew",
            current_period_start=self._dt(now - 30 * 86400),
            current_period_end=self._dt(now),
        )

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice(
                "sub_renew",
                invoice_id="in_renew",
                lines=[{"period": {"start": now, "end": now + 30 * 86400}}],
            ),
            succeeded=True,
        )

        sub.refresh_from_db()
        assert sub.current_period_start == self._dt(now)
        assert sub.current_period_end == self._dt(now + 30 * 86400)

    def test_revival_advances_from_the_previous_lifes_elapsed_period(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """A revival reuses the row, so its stale (elapsed) anchor must not block."""
        now = int(time.time())
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_revived",
            current_period_start=self._dt(now - 70 * 86400),
            current_period_end=self._dt(now - 40 * 86400),
            expired_at=self._dt(now - 40 * 86400),
        )

        subscription_stripe_sync.record_stripe_payment_from_invoice(
            _invoice(
                "sub_revived",
                invoice_id="in_revived",
                lines=[{"period": {"start": now, "end": now + 30 * 86400}}],
            ),
            succeeded=True,
        )

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.expired_at is None
        assert sub.current_period_start == self._dt(now)
        assert sub.current_period_end == self._dt(now + 30 * 86400)


class TestUnresolvedPaymentIntentIsNotPersisted:
    """An empty intent id must never overwrite one already stored.

    ``charge.refunded`` matches membership payments solely on the intent id, and
    the org-admin refund endpoint refuses ONLINE payments — so a row that loses
    its intent id can never be marked REFUNDED by any path.
    """

    def test_existing_intent_survives_a_failed_resolution(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_intent",
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=timezone.now() + datetime.timedelta(days=30),
            stripe_invoice_id="in_intent",
            stripe_payment_intent_id="pi_known",
        )

        invoice = _invoice("sub_intent", invoice_id="in_intent")
        invoice.pop("payment_intent")
        with patch(
            "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve",
            side_effect=stripe.error.APIConnectionError("boom"),
        ):
            subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        payment = MembershipPayment.objects.get(stripe_invoice_id="in_intent")
        assert payment.stripe_payment_intent_id == "pi_known"


class TestConnectAccountAuthorization:
    """Checkout metadata is attacker-choosable; the event's account is not."""

    @staticmethod
    def _event(session: dict[str, t.Any], account: str) -> MagicMock:
        mock_event = MagicMock(spec=stripe.Event)
        mock_event.type = "checkout.session.completed"
        mock_event.account = account
        mock_event.data = MagicMock()
        mock_event.data.object = session
        return mock_event

    def test_foreign_connected_account_cannot_link_a_row(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        org = online_plan.tier.organization
        org.stripe_account_id = "acct_victim"
        org.save(update_fields=["stripe_account_id"])
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        session = {
            "id": "cs_attacker",
            "mode": "subscription",
            "subscription": "sub_attacker",
            "metadata": {"membership_subscription_id": str(sub.pk)},
        }

        handler = StripeEventHandler(self._event(session, account="acct_attacker"))
        handler.handle_subscription_checkout_completed(handler.event)

        sub.refresh_from_db()
        assert not sub.stripe_subscription_id

    def test_owning_account_links_normally(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        org = online_plan.tier.organization
        org.stripe_account_id = "acct_owner"
        org.save(update_fields=["stripe_account_id"])
        sub = MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=org,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        session = {
            "id": "cs_ok",
            "mode": "subscription",
            "subscription": "sub_ok",
            "metadata": {"membership_subscription_id": str(sub.pk)},
        }

        handler = StripeEventHandler(self._event(session, account="acct_owner"))
        handler.handle_subscription_checkout_completed(handler.event)

        sub.refresh_from_db()
        assert sub.stripe_subscription_id == "sub_ok"


class TestStaffApprovalGuards:
    """A free staff approval must not clobber a paid or paused membership."""

    def test_refuses_when_the_user_holds_a_subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        organization: Organization,
    ) -> None:
        from ninja.errors import HttpError

        from events.service.organization_service import approve_membership_request

        MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        request = OrganizationMembershipRequest.objects.create(organization=organization, user=member_user)
        free_tier = MembershipTier.objects.get(organization=organization, name="General membership")

        with pytest.raises(HttpError):
            approve_membership_request(request, organization.owner, free_tier)

    def test_force_bypasses_the_guard(
        self,
        online_plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        organization: Organization,
    ) -> None:
        from events.service.organization_service import approve_membership_request

        MembershipSubscription.objects.create(
            user=member_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        request = OrganizationMembershipRequest.objects.create(organization=organization, user=member_user)
        free_tier = MembershipTier.objects.get(organization=organization, name="General membership")

        approve_membership_request(request, organization.owner, free_tier, force=True)

        request.refresh_from_db()
        assert request.status == OrganizationMembershipRequest.Status.COMPLETED
