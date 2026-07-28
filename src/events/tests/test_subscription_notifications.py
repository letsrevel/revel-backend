"""Tests for subscription notification context schemas + dispatch sites.

Dispatch-site tests are added in later tasks (D2/D3/E1). This file only
contains the type-registration smoke tests for now.
"""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service
from events.service.subscription_notifications import last_paid_amounts
from events.utils import format_organization_datetime
from notifications.context_schemas import (
    NOTIFICATION_CONTEXT_SCHEMAS,
    validate_notification_context,
)
from notifications.enums import NotificationType
from notifications.models import Notification, NotificationPreference


class TestSubscriptionNotificationTypes:
    def test_all_six_types_registered(self) -> None:
        for nt in [
            NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
            NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
            NotificationType.SUBSCRIPTION_EXPIRED,
            NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
            NotificationType.SUBSCRIPTION_RENEWAL_REMINDER,
            NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        ]:
            assert nt in NOTIFICATION_CONTEXT_SCHEMAS

    def test_renewal_succeeded_context_validates(self) -> None:
        validate_notification_context(
            NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
            {
                "organization_name": "Acme",
                "organization_slug": "acme",
                "plan_name": "Monthly",
                "amount": "10.00 EUR",
                "period_end": "2026-06-12",
            },
        )

    def test_renewal_succeeded_context_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Missing required context keys"):
            validate_notification_context(
                NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
                {"organization_name": "Acme"},
            )


@pytest.mark.django_db
class TestSubscriptionNotificationPreferencesDefaults:
    """Verify the six new subscription notification types default to enabled
    via NotificationPreference's fallback semantics — no data backfill needed.
    """

    def test_new_user_gets_all_six_types_enabled_by_default(self, nonmember_user: RevelUser) -> None:
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        for nt in [
            NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
            NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
            NotificationType.SUBSCRIPTION_EXPIRED,
            NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
            NotificationType.SUBSCRIPTION_RENEWAL_REMINDER,
            NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        ]:
            assert prefs.is_notification_type_enabled(nt.value), f"{nt} should default to enabled for new users"

    def test_legacy_user_with_partial_dict_still_gets_new_types_enabled(self, nonmember_user: RevelUser) -> None:
        """Simulate a pre-Phase-4 user whose notification_type_settings dict
        was populated before the new types existed. New types must still be
        enabled via the .get(type, {}).get('enabled', True) fallback."""
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        # Set settings to a dict that doesn't contain the new types
        prefs.notification_type_settings = {
            "ticket_created": {"enabled": True, "channels": ["email"]},
        }
        prefs.save(update_fields=["notification_type_settings"])
        assert prefs.is_notification_type_enabled(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED.value)

    def test_silence_all_still_disables_subscription_types(self, nonmember_user: RevelUser) -> None:
        prefs, _ = NotificationPreference.objects.get_or_create(user=nonmember_user)
        prefs.silence_all_notifications = True
        prefs.save(update_fields=["silence_all_notifications"])
        assert not prefs.is_notification_type_enabled(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED.value)


# ===========================================================================
# Fixtures and tests for D1: notification dispatch helpers
# ===========================================================================


@pytest.fixture
def helper_tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="HelperTier")


@pytest.fixture
def helper_plan(helper_tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=helper_tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
    )


@pytest.fixture
def helper_subscription(
    helper_plan: MembershipSubscriptionPlan,
    organization: Organization,
    nonmember_user: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=nonmember_user,
        plan=helper_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=timezone.now() - timedelta(days=10),
        current_period_end=timezone.now() + timedelta(days=20),
    )


@pytest.mark.django_db
class TestDispatchWiring:
    def test_dispatch_helper_enqueues_delivery_task_on_commit(
        self,
        helper_subscription: MembershipSubscription,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The helpers must route through the signal so the row is actually DELIVERED.

        Regression: the helpers used to call ``create_notification`` directly,
        which only inserts a Notification row with empty title/body — nothing
        ever enqueued ``dispatch_notification`` (the #442 create+on_commit
        pairing lives in the signal handler), so no email/Telegram went out and
        in-app entries rendered blank.
        """
        with mock.patch("notifications.tasks.dispatch_notification.delay") as mock_delay:
            with django_capture_on_commit_callbacks(execute=True):
                subscription_service._dispatch_renewal_succeeded(helper_subscription)
        notif = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        mock_delay.assert_called_once_with(str(notif.id))


@pytest.mark.django_db
class TestDispatchHelpers:
    def test_renewal_succeeded_creates_notification(self, helper_subscription: MembershipSubscription) -> None:
        subscription_service._dispatch_renewal_succeeded(helper_subscription)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["organization_name"] == helper_subscription.organization.name
        assert n.context["plan_name"] == helper_subscription.plan.name
        assert "10.00 EUR" in n.context["amount"]

    def test_renewal_succeeded_quotes_charged_amount_not_plan_price(
        self, helper_subscription: MembershipSubscription
    ) -> None:
        """A grandfathered subscriber's receipt must quote what they paid.

        Regression: the helper always quoted ``plan.price``. Raising a plan's
        price mints a NEW Stripe Price and leaves existing subscribers on the
        old one, so every one of them was told they had been charged the new
        (higher) figure.
        """
        assert helper_subscription.plan.price == Decimal("10.00")
        subscription_service._dispatch_renewal_succeeded(helper_subscription, amount=Decimal("8.00"), currency="EUR")
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["amount"] == "8.00 EUR"

    def test_renewal_succeeded_falls_back_to_plan_price_when_amount_unknown(
        self, helper_subscription: MembershipSubscription
    ) -> None:
        """Callers with no real figure keep the pre-existing behaviour."""
        subscription_service._dispatch_renewal_succeeded(helper_subscription, amount=None)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["amount"] == "10.00 EUR"

    def test_payment_failed_quotes_amount_at_stake(self, helper_subscription: MembershipSubscription) -> None:
        """The failed-payment warning quotes the invoice's amount, not plan.price."""
        subscription_service._dispatch_payment_failed(
            helper_subscription,
            grace_period_end=timezone.now() + timedelta(days=7),
            is_online=True,
            amount=Decimal("8.00"),
            currency="EUR",
        )
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        )
        assert n.context["amount"] == "8.00 EUR"

    def test_payment_failed_includes_is_online(self, helper_subscription: MembershipSubscription) -> None:
        subscription_service._dispatch_payment_failed(
            helper_subscription,
            grace_period_end=timezone.now() + timedelta(days=7),
            is_online=True,
        )
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        )
        assert n.context["is_online"] is True
        assert "grace_period_end" in n.context

    def test_expired_includes_revival_window_when_within(self, helper_subscription: MembershipSubscription) -> None:
        helper_subscription.expired_at = timezone.now() - timedelta(days=5)
        helper_subscription.save(update_fields=["expired_at"])
        subscription_service._dispatch_subscription_expired(helper_subscription)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        )
        assert n.context.get("revival_url") is not None
        assert n.context.get("revival_window_end") is not None

    def test_expired_omits_revival_when_window_zero(
        self, helper_subscription: MembershipSubscription, organization: Organization
    ) -> None:
        organization.membership_subscription_revival_window_days = 0
        organization.save(update_fields=["membership_subscription_revival_window_days"])
        helper_subscription.expired_at = timezone.now()
        helper_subscription.save(update_fields=["expired_at"])
        subscription_service._dispatch_subscription_expired(helper_subscription)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        )
        assert "revival_url" not in n.context
        assert "revival_window_end" not in n.context

    def test_cancellation_confirmed_immediate(self, helper_subscription: MembershipSubscription) -> None:
        subscription_service._dispatch_cancellation_confirmed(helper_subscription, immediate=True)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        )
        assert n.context["immediate"] is True

    def test_cancellation_confirmed_at_period_end(self, helper_subscription: MembershipSubscription) -> None:
        subscription_service._dispatch_cancellation_confirmed(helper_subscription, immediate=False)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        )
        assert n.context["immediate"] is False
        assert helper_subscription.current_period_end is not None
        # Org-local, human-readable (#511/#542) — never a raw UTC isoformat.
        assert n.context["access_ends_at"] == format_organization_datetime(
            helper_subscription.current_period_end, helper_subscription.organization
        )

    def test_renewal_succeeded_offline_omits_manage_subscription_url(
        self, helper_subscription: MembershipSubscription
    ) -> None:
        """OFFLINE plans have no self-service billing page, so the CTA is absent."""
        assert helper_subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE.value
        subscription_service._dispatch_renewal_succeeded(helper_subscription)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert "manage_subscription_url" not in n.context

    def test_renewal_succeeded_online_includes_manage_subscription_url(
        self, helper_plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """ONLINE plans carry a manage_subscription_url pointing at the FE subscription page."""
        helper_plan.payment_method = MembershipSubscriptionPlan.PaymentMethod.ONLINE
        helper_plan.save(update_fields=["payment_method"])
        sub = MembershipSubscription.objects.create(
            user=nonmember_user,
            plan=helper_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now() - timedelta(days=10),
            current_period_end=timezone.now() + timedelta(days=20),
        )
        subscription_service._dispatch_renewal_succeeded(sub)
        n = Notification.objects.get(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["manage_subscription_url"].endswith(f"/org/{organization.slug}/subscription")

    def test_expired_omits_revival_when_expired_at_none(self, helper_subscription: MembershipSubscription) -> None:
        assert helper_subscription.expired_at is None
        subscription_service._dispatch_subscription_expired(helper_subscription)
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        )
        assert "revival_url" not in n.context
        assert "revival_window_end" not in n.context

    def test_price_migration_includes_old_and_new(self, helper_subscription: MembershipSubscription) -> None:
        subscription_service._dispatch_price_migration(
            helper_subscription,
            old_price=Decimal("10.00"),
            new_price=Decimal("12.00"),
        )
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        )
        assert "10.00 EUR" in n.context["old_amount"]
        assert "12.00 EUR" in n.context["new_amount"]


# ===========================================================================
# D2: OFFLINE dispatch sites — gating logic
# ===========================================================================


@pytest.mark.django_db
class TestOfflineDispatchSites:
    def test_renewal_succeeded_fires_on_active_renewal(
        self,
        helper_subscription: MembershipSubscription,
        helper_plan: MembershipSubscriptionPlan,
        nonmember_user: RevelUser,
    ) -> None:
        """ACTIVE subscription receiving a payment → RENEWAL_SUCCEEDED fires."""
        assert helper_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        subscription_service.record_payment(
            helper_subscription,
            amount=helper_plan.price,
            currency=helper_plan.currency,
            recorded_by=None,
        )
        assert Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        ).exists()

    def test_renewal_succeeded_quotes_the_recorded_amount(
        self,
        helper_subscription: MembershipSubscription,
        nonmember_user: RevelUser,
    ) -> None:
        """OFFLINE staff pick the amount — the receipt must quote *that*.

        Regression: ``record_payment`` accepts an arbitrary amount (a discount,
        a grandfathered price, a part payment) but the receipt always quoted
        ``plan.price``, telling the member they paid a sum they never did.
        """
        subscription_service.record_payment(
            helper_subscription,
            amount=Decimal("7.50"),
            currency="EUR",
            recorded_by=None,
        )
        n = Notification.objects.get(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["amount"] == "7.50 EUR"

    def test_renewal_succeeded_fires_on_past_due_renewal(
        self,
        helper_subscription: MembershipSubscription,
        helper_plan: MembershipSubscriptionPlan,
        nonmember_user: RevelUser,
    ) -> None:
        """PAST_DUE subscription receiving a payment → RENEWAL_SUCCEEDED fires."""
        helper_subscription.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        helper_subscription.save(update_fields=["status"])
        subscription_service.record_payment(
            helper_subscription,
            amount=helper_plan.price,
            currency=helper_plan.currency,
            recorded_by=None,
        )
        assert Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        ).exists()

    def test_renewal_succeeded_skipped_on_first_payment(
        self,
        helper_plan: MembershipSubscriptionPlan,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """PENDING subscription (first payment) → no RENEWAL_SUCCEEDED (not a renewal)."""
        sub = MembershipSubscription.objects.create(
            user=nonmember_user,
            plan=helper_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        subscription_service.record_payment(
            sub,
            amount=helper_plan.price,
            currency=helper_plan.currency,
            recorded_by=None,
        )
        assert not Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        ).exists()

    def test_renewal_succeeded_skipped_when_dispatch_flag_off(
        self,
        helper_subscription: MembershipSubscription,
        helper_plan: MembershipSubscriptionPlan,
        nonmember_user: RevelUser,
    ) -> None:
        """dispatch_renewal_notification=False suppresses the notification."""
        subscription_service.record_payment(
            helper_subscription,
            amount=helper_plan.price,
            currency=helper_plan.currency,
            recorded_by=None,
            dispatch_renewal_notification=False,
        )
        assert not Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        ).exists()

    def test_cancellation_confirmed_immediate_offline(
        self,
        helper_subscription: MembershipSubscription,
        nonmember_user: RevelUser,
    ) -> None:
        """Immediate cancel from ACTIVE → CANCELLATION_CONFIRMED with immediate=True."""
        subscription_service.cancel_subscription(helper_subscription, immediate=True)
        notifs = Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        )
        assert notifs.count() == 1
        assert notifs.first().context["immediate"] is True  # type: ignore[union-attr]

    def test_cancellation_confirmed_at_period_end_fires_once(
        self,
        helper_subscription: MembershipSubscription,
        nonmember_user: RevelUser,
    ) -> None:
        """at-period-end cancel fires once; idempotent re-call does not re-fire."""
        subscription_service.cancel_subscription(helper_subscription, immediate=False)
        subscription_service.cancel_subscription(helper_subscription, immediate=False)  # idempotent
        notifs = Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        )
        assert notifs.count() == 1
        assert notifs.first().context["immediate"] is False  # type: ignore[union-attr]


# ===========================================================================
# D2 extended: ONLINE cancel dispatch — gating logic
# ===========================================================================


@pytest.mark.django_db
class TestOnlineCancelDispatch:
    def test_online_cancel_immediate_fires_cancellation_confirmed(
        self,
        helper_tier: MembershipTier,
        organization: Organization,
        nonmember_user: RevelUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ONLINE cancel routes through cancel_online_subscription but must
        still fire CANCELLATION_CONFIRMED exactly once (the local-side gate)."""
        from events.models import CustomerProfile
        from events.service import subscription_stripe_service

        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=helper_tier,
            name="Online",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_x",
            stripe_product_id="prod_x",
        )
        CustomerProfile.objects.create(
            user=nonmember_user,
            organization=organization,
            stripe_customer_id="cus_test",
        )
        sub = MembershipSubscription.objects.create(
            user=nonmember_user,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_test_xyz",
            current_period_start=timezone.now() - timedelta(days=10),
            current_period_end=timezone.now() + timedelta(days=20),
        )

        def fake_cancel_online(subscription: MembershipSubscription, *, immediate: bool) -> MembershipSubscription:
            # Mirror what cancel_online_subscription does locally
            subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
            subscription.cancelled_at = timezone.now()
            subscription.cancel_at_period_end = False
            subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
            return subscription

        monkeypatch.setattr(subscription_stripe_service, "cancel_online_subscription", fake_cancel_online)
        subscription_service.cancel_subscription(sub, immediate=True)

        notifs = Notification.objects.filter(
            user=nonmember_user,
            notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        )
        assert notifs.count() == 1
        assert notifs.first().context["immediate"] is True  # type: ignore[union-attr]


# ===========================================================================
# Last-paid-amount anchor (renewal reminders quote it instead of plan.price)
# ===========================================================================


@pytest.mark.django_db
class TestLastPaidAmounts:
    @staticmethod
    def _pay(
        subscription: MembershipSubscription,
        amount: str,
        *,
        billing_reason: str,
        created_at: datetime,
        status: str = MembershipPayment.PaymentStatus.SUCCEEDED,
    ) -> MembershipPayment:
        payment = MembershipPayment.objects.create(
            subscription=subscription,
            amount=Decimal(amount),
            currency="EUR",
            status=status,
            period_start=created_at,
            period_end=created_at + timedelta(days=30),
            raw_response={"billing_reason": billing_reason},
        )
        # created_at is auto_now_add — pin it so DISTINCT ON's ordering is exact.
        MembershipPayment.objects.filter(pk=payment.pk).update(created_at=created_at)
        return payment

    def test_returns_latest_non_proration_succeeded_amount(self, helper_subscription: MembershipSubscription) -> None:
        now = timezone.now()
        self._pay(helper_subscription, "8.00", billing_reason="subscription_cycle", created_at=now - timedelta(days=60))
        self._pay(helper_subscription, "9.00", billing_reason="subscription_cycle", created_at=now - timedelta(days=30))
        # A mid-cycle upgrade's proration is a partial-period delta, never the
        # subscriber's per-period price — it must not become the anchor.
        self._pay(helper_subscription, "1.50", billing_reason="subscription_update", created_at=now - timedelta(days=1))
        # Nor may a failed attempt.
        self._pay(
            helper_subscription,
            "99.00",
            billing_reason="subscription_cycle",
            created_at=now,
            status=MembershipPayment.PaymentStatus.FAILED,
        )

        assert last_paid_amounts([helper_subscription]) == {helper_subscription.id: Decimal("9.00")}

    def test_subscription_without_payments_is_absent(self, helper_subscription: MembershipSubscription) -> None:
        assert last_paid_amounts([helper_subscription]) == {}
