"""Tests for subscription notification context schemas + dispatch sites.

Dispatch-site tests are added in later tasks (D2/D3/E1). This file only
contains the type-registration smoke tests for now.
"""

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
from events.service import subscription_service
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
        assert n.context["access_ends_at"] == helper_subscription.current_period_end.isoformat()

    def test_renewal_succeeded_includes_customer_portal_url_when_provided(
        self, helper_subscription: MembershipSubscription
    ) -> None:
        subscription_service._dispatch_renewal_succeeded(
            helper_subscription, customer_portal_url="https://billing.example.com"
        )
        n = Notification.objects.get(
            user=helper_subscription.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        )
        assert n.context["customer_portal_url"] == "https://billing.example.com"

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
