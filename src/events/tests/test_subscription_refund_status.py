"""The refund webhooks read the Stripe refund status for membership payments (#1507).

A pending refund must not mark the payment REFUNDED and must not cancel the subscription. A refund
that fails later reopens the payment, alerts staff, and leaves the subscription cancelled.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.stripe_webhooks import handle_event
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

INTENT = "pi_member_1507"
_FAILURE_REASON = "expired_or_canceled_card"


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="m_sub", email="m_sub@example.com", password="pass")


@pytest.fixture
def plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
    )


@pytest.fixture
def sub(plan: MembershipSubscriptionPlan, organization: Organization, subscriber: RevelUser) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=timezone.now() - timedelta(days=10),
        current_period_end=timezone.now() + timedelta(days=20),
    )


@pytest.fixture
def payment(sub: MembershipSubscription, plan: MembershipSubscriptionPlan) -> MembershipPayment:
    assert sub.current_period_start is not None
    assert sub.current_period_end is not None
    return MembershipPayment.objects.create(
        subscription=sub,
        amount=plan.price,
        currency=plan.currency,
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
        stripe_payment_intent_id=INTENT,
    )


def _refund(refund_id: str, amount: int, status: str, failure_reason: str | None = None) -> dict[str, t.Any]:
    return {
        "object": "refund",
        "id": refund_id,
        "amount": amount,
        "status": status,
        "failure_reason": failure_reason,
        "payment_intent": INTENT,
        "charge": "ch_member_1507",
        "metadata": {},
    }


def _charge_refunded(refunds: list[dict[str, t.Any]], event_id: str, amount_refunded: int) -> stripe.Event:
    charge = {
        "id": "ch_member_1507",
        "payment_intent": INTENT,
        "amount": 1000,
        "amount_refunded": amount_refunded,
        "refunds": {"data": refunds},
    }
    payload = {"id": event_id, "type": "charge.refunded", "livemode": False, "data": {"object": charge}}
    return stripe.Event.construct_from(payload, "sk_test")


def _refund_event(event_type: str, refund: dict[str, t.Any], event_id: str) -> stripe.Event:
    payload = {"id": event_id, "type": event_type, "livemode": False, "data": {"object": refund}}
    return stripe.Event.construct_from(payload, "sk_test")


def _deliver(event: stripe.Event, capture: t.Any, listed: list[dict[str, t.Any]] | None = None) -> t.Any:
    """Run the webhook; ``listed`` is what ``stripe.Refund.list`` returns for the charge."""
    with (
        patch.object(stripe.Refund, "list") as list_mock,
        patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
    ):
        list_mock.return_value.auto_paging_iter.return_value = listed or []
        with capture_logs() as logs:
            with capture(execute=True):
                handle_event(event)
    return logs


def _lines(logs: t.Any, name: str) -> list[t.Any]:
    return [line for line in logs if line["event"] == name]


class TestPendingRefund:
    def test_a_pending_full_refund_does_not_cancel_the_subscription(
        self, payment: MembershipPayment, sub: MembershipSubscription, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        event = _charge_refunded([_refund("re_m", 1000, "pending")], "evt_m1", amount_refunded=1000)
        _deliver(event, django_capture_on_commit_callbacks)

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_the_subscription_is_cancelled_when_the_pending_refund_succeeds(
        self, payment: MembershipPayment, sub: MembershipSubscription, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        pending = _charge_refunded([_refund("re_m", 1000, "pending")], "evt_m2", amount_refunded=1000)
        _deliver(pending, django_capture_on_commit_callbacks)

        done = _refund("re_m", 1000, "succeeded")
        _deliver(_refund_event("refund.updated", done, "evt_m3"), django_capture_on_commit_callbacks, listed=[done])

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        assert payment.stripe_refund_id == "re_m"
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED


class TestLateFailure:
    def test_a_full_refund_that_fails_later_reopens_the_payment_and_alerts_staff(
        self,
        payment: MembershipPayment,
        sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        done = _charge_refunded([_refund("re_m", 1000, "succeeded")], "evt_m4", amount_refunded=1000)
        _deliver(done, django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        seen = set(Notification.objects.values_list("pk", flat=True))

        failed = _refund("re_m", 1000, "failed", _FAILURE_REASON)
        logs = _deliver(
            _refund_event("refund.failed", failed, "evt_m5"), django_capture_on_commit_callbacks, listed=[failed]
        )

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        # The subscription stays cancelled: the member subscribes again if wanted.
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        (line,) = _lines(logs, "membership_refund_failed")
        assert line == {
            "event": "membership_refund_failed",
            "log_level": "error",
            "payment_id": str(payment.pk),
            "subscription_id": str(sub.pk),
            "payment_intent_id": INTENT,
            "organization_id": str(organization.pk),
            "refund_id": "re_m",
            "status": "failed",
            "failure_reason": _FAILURE_REASON,
            "previous_status": "refunded",
        }
        new = Notification.objects.exclude(pk__in=seen)
        staff = new.filter(notification_type=NotificationType.REFUND_FAILED)
        assert list(staff.values_list("user_id", flat=True)) == [organization_owner_user.pk]
        assert staff.get().context["kind"] == "membership"
        # No earlier "refunded" email exists for a membership, so the member is not told.
        assert not new.filter(notification_type=NotificationType.TICKET_REFUND_FAILED).exists()
        assert not new.filter(user=subscriber, notification_type=NotificationType.PAYMENT_CONFIRMATION).exists()

    def test_a_partial_refund_that_fails_later_clears_the_recorded_amount(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        partial = _charge_refunded([_refund("re_p", 250, "succeeded")], "evt_p1", amount_refunded=250)
        _deliver(partial, django_capture_on_commit_callbacks)
        assert MembershipPayment.objects.get(pk=payment.pk).refund_amount == Decimal("2.50")

        failed = _refund("re_p", 250, "failed", _FAILURE_REASON)
        logs = _deliver(
            _refund_event("refund.failed", failed, "evt_p2"), django_capture_on_commit_callbacks, listed=[failed]
        )

        payment.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        assert len(_lines(logs, "membership_refund_failed")) == 1

    def test_refund_failed_and_refund_updated_alert_once(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _deliver(
            _charge_refunded([_refund("re_m", 1000, "succeeded")], "evt_o1", amount_refunded=1000),
            django_capture_on_commit_callbacks,
        )
        failed = _refund("re_m", 1000, "failed", _FAILURE_REASON)

        first = _deliver(
            _refund_event("refund.failed", failed, "evt_o2"), django_capture_on_commit_callbacks, listed=[failed]
        )
        second = _deliver(
            _refund_event("refund.updated", failed, "evt_o3"), django_capture_on_commit_callbacks, listed=[failed]
        )

        assert len(_lines(first, "membership_refund_failed")) == 1
        assert not _lines(second, "membership_refund_failed")


class TestStaleAndConcurrentEvents:
    def test_a_stale_succeeded_event_does_not_override_the_listed_failure(
        self, payment: MembershipPayment, sub: MembershipSubscription, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        stale = _refund("re_m", 1000, "succeeded")
        listed = _refund("re_m", 1000, "failed", _FAILURE_REASON)

        _deliver(_refund_event("refund.updated", stale, "evt_s1"), django_capture_on_commit_callbacks, listed=[listed])

        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_a_stale_failure_event_does_not_reopen_a_listed_success(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _deliver(
            _charge_refunded([_refund("re_m", 1000, "succeeded")], "evt_s2", amount_refunded=1000),
            django_capture_on_commit_callbacks,
        )
        stale = _refund("re_m", 1000, "failed", _FAILURE_REASON)
        listed = _refund("re_m", 1000, "succeeded")

        logs = _deliver(
            _refund_event("refund.failed", stale, "evt_s3"), django_capture_on_commit_callbacks, listed=[listed]
        )

        payment.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        assert not _lines(logs, "membership_refund_failed")

    def test_a_late_success_event_never_lowers_the_recorded_refund_amount(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        payment.refund_amount = Decimal("6.00")  # refunds A (2.50) and B (3.50) settled already
        payment.save(update_fields=["refund_amount"])
        only_a = _refund("re_a", 250, "succeeded")

        _deliver(_refund_event("refund.updated", only_a, "evt_s4"), django_capture_on_commit_callbacks, listed=[only_a])

        payment.refresh_from_db()
        assert payment.refund_amount == Decimal("6.00")

    def test_the_refund_event_locks_only_the_membership_payment_row(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        done = _refund("re_m", 1000, "succeeded")
        with CaptureQueriesContext(connection) as queries:
            _deliver(_refund_event("refund.updated", done, "evt_l1"), django_capture_on_commit_callbacks, listed=[done])

        (locking,) = [
            q["sql"]
            for q in queries
            if "FOR UPDATE" in q["sql"] and 'FROM "events_membershippayment" INNER JOIN' in q["sql"]
        ]
        assert 'FOR UPDATE OF "events_membershippayment"' in locking

    def test_the_refund_list_is_read_under_the_membership_payment_lock(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """A failure that commits between the list and the lock would otherwise be overwritten."""
        done = _refund("re_m", 1000, "succeeded")
        queries_before_list: list[int] = []

        with CaptureQueriesContext(connection) as queries:

            def list_refunds(**kwargs: t.Any) -> MagicMock:
                queries_before_list.append(len(queries.captured_queries))
                result = MagicMock()
                result.auto_paging_iter.return_value = [done]
                return result

            with (
                patch.object(stripe.Refund, "list", side_effect=list_refunds),
                patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
                django_capture_on_commit_callbacks(execute=True),
            ):
                handle_event(_refund_event("refund.updated", done, "evt_l2"))

        lock_index = next(
            i
            for i, q in enumerate(queries.captured_queries)
            if "FOR UPDATE" in q["sql"] and 'FROM "events_membershippayment" INNER JOIN' in q["sql"]
        )
        (listed_after,) = queries_before_list
        assert listed_after > lock_index


class TestSubscriptionCancelledFlag:
    def test_a_failed_full_refund_flags_the_cancelled_subscription(
        self,
        payment: MembershipPayment,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _deliver(
            _charge_refunded([_refund("re_m", 1000, "succeeded")], "evt_c1", amount_refunded=1000),
            django_capture_on_commit_callbacks,
        )
        failed = _refund("re_m", 1000, "failed", _FAILURE_REASON)
        _deliver(_refund_event("refund.failed", failed, "evt_c2"), django_capture_on_commit_callbacks, listed=[failed])

        notification = Notification.objects.get(notification_type=NotificationType.REFUND_FAILED)
        assert notification.context["subscription_cancelled_by_refund"] is True

    def test_a_failed_pending_refund_does_not_flag_it(
        self, payment: MembershipPayment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        failed = _refund("re_m", 1000, "failed", _FAILURE_REASON)
        _deliver(_refund_event("refund.failed", failed, "evt_c3"), django_capture_on_commit_callbacks, listed=[failed])

        notification = Notification.objects.get(notification_type=NotificationType.REFUND_FAILED)
        assert notification.context["subscription_cancelled_by_refund"] is False

    def test_a_failed_refund_of_a_past_period_payment_does_not_flag_it(
        self,
        payment: MembershipPayment,
        sub: MembershipSubscription,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A renewed member: the refunded payment is of an old period, so the subscription stays ACTIVE."""
        sub.current_period_start = payment.period_end
        sub.current_period_end = payment.period_end + timedelta(days=30)
        sub.save(update_fields=["current_period_start", "current_period_end"])
        _deliver(
            _charge_refunded([_refund("re_m", 1000, "succeeded")], "evt_p1", amount_refunded=1000),
            django_capture_on_commit_callbacks,
        )
        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

        failed = _refund("re_m", 1000, "failed", _FAILURE_REASON)
        _deliver(_refund_event("refund.failed", failed, "evt_p2"), django_capture_on_commit_callbacks, listed=[failed])

        notification = Notification.objects.get(notification_type=NotificationType.REFUND_FAILED)
        assert notification.context["subscription_cancelled_by_refund"] is False
