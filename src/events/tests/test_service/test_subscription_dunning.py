"""D3 webhook dispatch tests for ONLINE subscription notifications.

Covers the notification gates added to ``sync_subscription_from_stripe`` and
``record_stripe_payment_from_invoice`` in Phase 4 D3.  Each test verifies that
the dispatch is either triggered or suppressed depending on the prior vs. final
local state.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_stripe_sync
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


# ---- Helpers / fixtures ------------------------------------------------------


def _make_stripe_connected(org: Organization) -> None:
    org.stripe_account_id = "acct_dunning_test"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    _make_stripe_connected(organization)
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=stripe_org, name="General membership")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online Dunning",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_dunning",
        stripe_price_id="price_dunning",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="dunning_subscriber", email="dunning@example.com", password="pass"
    )


def _make_online_sub(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    stripe_id: str,
    status: MembershipSubscription.SubscriptionStatus = MembershipSubscription.SubscriptionStatus.ACTIVE,
    cancel_at_period_end: bool = False,
) -> MembershipSubscription:
    now = timezone.now()
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=plan.tier.organization,
        status=status,
        stripe_subscription_id=stripe_id,
        cancel_at_period_end=cancel_at_period_end,
        current_period_start=now - timedelta(days=15),
        current_period_end=now + timedelta(days=15),
    )


def _stripe_sub_payload(
    stripe_id: str,
    *,
    status: str = "active",
    cancel_at_period_end: bool = False,
) -> dict[str, t.Any]:
    now_epoch = int(timezone.now().timestamp())
    return {
        "id": stripe_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_start": now_epoch - 15 * 86400,
        "current_period_end": now_epoch + 15 * 86400,
        "items": {"data": [{"price": {"id": "price_dunning"}}]},
    }


def _invoice_payload(stripe_sub_id: str, *, invoice_id: str, succeeded: bool) -> dict[str, t.Any]:
    now_epoch = int(timezone.now().timestamp())
    return {
        "id": invoice_id,
        "subscription": stripe_sub_id,
        "amount_paid": 1000 if succeeded else 0,
        "currency": "eur",
        "payment_intent": "pi_dunning_test",
        "lines": {"data": [{"period": {"start": now_epoch - 86400, "end": now_epoch + 30 * 86400}}]},
    }


def _has_notification(user: RevelUser, nt: NotificationType) -> bool:
    return Notification.objects.filter(user=user, notification_type=nt).count() == 1


# ---- invoice.paid — RENEWAL_SUCCEEDED ----------------------------------------


class TestInvoicePaidRenewal:
    def test_active_to_active_renewal_fires_renewal_succeeded(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """invoice.paid with prior_status=ACTIVE must dispatch RENEWAL_SUCCEEDED."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_ren1")
        invoice = _invoice_payload("sub_ren1", invoice_id="in_ren1", succeeded=True)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)
        # ONLINE renewal_succeeded must carry the self-service management URL so
        # the "Manage Billing" CTA renders (dead branch before this fix).
        notif = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED
        )
        assert notif.context["manage_subscription_url"].endswith(
            f"/organizations/{online_plan.tier.organization.slug}/subscription"
        )

    def test_past_due_to_active_renewal_fires_renewal_succeeded(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """invoice.paid after dunning recovery (PAST_DUE → ACTIVE) must also fire RENEWAL_SUCCEEDED."""
        _make_online_sub(
            online_plan, subscriber, stripe_id="sub_ren2", status=MembershipSubscription.SubscriptionStatus.PAST_DUE
        )
        invoice = _invoice_payload("sub_ren2", invoice_id="in_ren2", succeeded=True)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)

    def test_pending_first_payment_does_not_fire_renewal_succeeded(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """invoice.paid for the very first payment (PENDING → ACTIVE) must NOT fire RENEWAL_SUCCEEDED."""
        _make_online_sub(
            online_plan, subscriber, stripe_id="sub_ren3", status=MembershipSubscription.SubscriptionStatus.PENDING
        )
        invoice = _invoice_payload("sub_ren3", invoice_id="in_ren3", succeeded=True)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)

    def test_invoice_paid_redelivery_does_not_double_fire_renewal_succeeded(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Stripe re-delivers invoice.paid. Second handler invocation must not
        fire a second RENEWAL_SUCCEEDED notification."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_ren4")
        invoice = _invoice_payload("sub_ren4", invoice_id="in_ren4", succeeded=True)

        # First delivery
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
        # Re-delivery of the same invoice
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)

    def test_dunning_recovery_on_same_invoice_fires_renewal_succeeded(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A dunning/SCA recovery flips the same invoice FAILED → SUCCEEDED and must fire once.

        ``invoice.payment_failed`` creates a FAILED row and moves the sub to
        PAST_DUE. When the member completes 3DS / Stripe's retry succeeds,
        ``invoice.paid`` arrives for the SAME invoice id — ``update_or_create``
        matches (created=False), so the plain payment_created gate would never
        announce the recovery. The ``payment_recovered`` gate must still fire
        RENEWAL_SUCCEEDED exactly once.
        """
        sub = _make_online_sub(online_plan, subscriber, stripe_id="sub_recover")
        failed_invoice = _invoice_payload("sub_recover", invoice_id="in_recover", succeeded=False)

        subscription_stripe_sync.record_stripe_payment_from_invoice(failed_invoice, succeeded=False)
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)

        paid_invoice = _invoice_payload("sub_recover", invoice_id="in_recover", succeeded=True)
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(paid_invoice, succeeded=True)

        assert payment is not None
        assert payment.status == payment.PaymentStatus.SUCCEEDED
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)


# ---- invoice.payment_failed — PAYMENT_FAILED ---------------------------------


class TestInvoicePaymentFailed:
    def test_active_to_past_due_fires_payment_failed(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """invoice.payment_failed with prior_status=ACTIVE must dispatch PAYMENT_FAILED."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_fail1")
        invoice = _invoice_payload("sub_fail1", invoice_id="in_fail1", succeeded=False)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)
        # ONLINE payment_failed must carry the management URL + is_online so the
        # "Update Payment Method" branch renders instead of the contact fallback.
        notif = Notification.objects.get(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_PAYMENT_FAILED
        )
        assert notif.context["is_online"] is True
        assert notif.context["manage_subscription_url"].endswith(
            f"/organizations/{online_plan.tier.organization.slug}/subscription"
        )

    def test_already_past_due_redelivery_does_not_double_fire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Webhook re-delivery when already PAST_DUE must not fire a second PAYMENT_FAILED."""
        _make_online_sub(
            online_plan, subscriber, stripe_id="sub_fail2", status=MembershipSubscription.SubscriptionStatus.PAST_DUE
        )
        invoice = _invoice_payload("sub_fail2", invoice_id="in_fail2", succeeded=False)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)

    def test_second_failure_on_same_invoice_does_not_double_fire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A second payment_failed for the same invoice (created=False) must stay suppressed.

        Guards that the recovery fix did not loosen the failure branch: the row
        already exists, so ``payment_created`` is False and no second
        PAYMENT_FAILED should be sent.
        """
        _make_online_sub(online_plan, subscriber, stripe_id="sub_fail3")
        invoice = _invoice_payload("sub_fail3", invoice_id="in_fail3", succeeded=False)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)
        # Same invoice re-fails (created=False on the second pass).
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        # Exactly one PAYMENT_FAILED — the helper asserts count == 1.
        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)

    def test_payment_action_required_routes_through_failed_branch(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """invoice.payment_action_required (SCA-blocked renewal) must dun like a failure.

        The invoice stays open with no immediate ``payment_failed``, so this
        event is the only prompt signal to move the sub to PAST_DUE and tell
        the member to complete 3DS confirmation via the portal link.
        """
        from unittest import mock

        sub = _make_online_sub(online_plan, subscriber, stripe_id="sub_sca1")
        invoice = _invoice_payload("sub_sca1", invoice_id="in_sca1", succeeded=False)

        event = mock.Mock()
        event.type = "invoice.payment_action_required"
        event.id = "evt_sca_1"
        event.data.object = invoice
        from events.service.stripe_webhooks import StripeEventHandler

        handled = StripeEventHandler(event).handle()

        assert handled is True
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAST_DUE
        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)


# ---- customer.subscription.updated cancel_at_period_end ----------------------


class TestSyncCancelAtPeriodEnd:
    def test_false_to_true_fires_cancellation_confirmed_not_immediate(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """First False→True transition must fire CANCELLATION_CONFIRMED(immediate=False)."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_cap1", cancel_at_period_end=False)
        payload = _stripe_sub_payload("sub_cap1", status="active", cancel_at_period_end=True)

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        notif = Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        ).first()
        assert notif is not None
        assert notif.context.get("immediate") is False

    def test_already_true_echo_does_not_fire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """If cancel_at_period_end is already True (echo of local cancel), no notification."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_cap2", cancel_at_period_end=True)
        payload = _stripe_sub_payload("sub_cap2", status="active", cancel_at_period_end=True)

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED)


# ---- customer.subscription.deleted — CANCELLATION_CONFIRMED(immediate=True) --


class TestSyncSubscriptionDeleted:
    def test_non_terminal_to_cancelled_fires_cancellation_confirmed_immediate(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Stripe 'canceled' event when local row is ACTIVE must fire CANCELLATION_CONFIRMED(immediate=True)."""
        _make_online_sub(online_plan, subscriber, stripe_id="sub_del1")
        payload = _stripe_sub_payload("sub_del1", status="canceled")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        notif = Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        ).first()
        assert notif is not None
        assert notif.context.get("immediate") is True

    def test_past_due_deleted_lands_expired_with_revival_window(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Stripe dunning exhausting before the local grace clock is an involuntary lapse.

        ``customer.subscription.deleted`` (status "canceled") arriving on a
        PAST_DUE row without a member-chosen cancel must land EXPIRED — with
        ``expired_at`` stamped so the revival window opens — and fire
        SUBSCRIPTION_EXPIRED, not a cancellation confirmation (ADR-0014/0015).
        """
        sub = _make_online_sub(
            online_plan,
            subscriber,
            stripe_id="sub_dun_exhausted",
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        )
        payload = _stripe_sub_payload("sub_dun_exhausted", status="canceled")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.expired_at is not None
        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_EXPIRED)
        assert not Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        ).exists()

    def test_past_due_deleted_with_chosen_cancel_lands_cancelled(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A member-chosen scheduled cancel reaching its end is NOT an involuntary lapse."""
        sub = _make_online_sub(
            online_plan,
            subscriber,
            stripe_id="sub_dun_chosen",
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
            cancel_at_period_end=True,
        )
        payload = _stripe_sub_payload("sub_dun_chosen", status="canceled", cancel_at_period_end=True)

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert sub.expired_at is None
        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_EXPIRED)

    def test_sync_back_to_active_clears_stale_expired_at(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A revival row confirming ACTIVE via sync consumes its old expiry."""
        sub = _make_online_sub(
            online_plan,
            subscriber,
            stripe_id="sub_revival_confirm",
            status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        )
        sub.expired_at = timezone.now() - timedelta(days=3)
        sub.save(update_fields=["expired_at"])
        payload = _stripe_sub_payload("sub_revival_confirm", status="active")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.expired_at is None

    def test_already_cancelled_echo_does_not_fire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """If local row is already CANCELLED (echo of D2 local cancel), no notification."""
        sub = _make_online_sub(
            online_plan,
            subscriber,
            stripe_id="sub_del2",
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
        )
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["cancelled_at"])
        payload = _stripe_sub_payload("sub_del2", status="canceled")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED)

    def test_scheduled_cancel_reaching_period_end_does_not_refire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Period-end deletion of a scheduled cancel must not re-fire as immediate=True.

        The member already received CANCELLATION_CONFIRMED(immediate=False) when
        they scheduled the cancel; the ``customer.subscription.deleted`` webhook
        at the period boundary must stay silent instead of sending a false
        "cancelled effective immediately" message.
        """
        _make_online_sub(online_plan, subscriber, stripe_id="sub_del3", cancel_at_period_end=True)
        payload = _stripe_sub_payload("sub_del3", status="canceled", cancel_at_period_end=True)

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED)


# ---- customer.subscription.updated incomplete_expired — SUBSCRIPTION_EXPIRED -


class TestSyncSubscriptionExpired:
    def test_non_terminal_to_expired_fires_subscription_expired(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Stripe 'incomplete_expired' event must dispatch SUBSCRIPTION_EXPIRED."""
        _make_online_sub(
            online_plan, subscriber, stripe_id="sub_exp1", status=MembershipSubscription.SubscriptionStatus.PENDING
        )
        payload = _stripe_sub_payload("sub_exp1", status="incomplete_expired")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        assert _has_notification(subscriber, NotificationType.SUBSCRIPTION_EXPIRED)

    def test_already_expired_echo_does_not_fire(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Re-delivery when local row is already EXPIRED must not fire again."""
        sub = _make_online_sub(
            online_plan,
            subscriber,
            stripe_id="sub_exp2",
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
        )
        sub.expired_at = timezone.now()
        sub.save(update_fields=["expired_at"])
        payload = _stripe_sub_payload("sub_exp2", status="incomplete_expired")

        subscription_stripe_sync.sync_subscription_from_stripe(payload)

        assert not _has_notification(subscriber, NotificationType.SUBSCRIPTION_EXPIRED)
