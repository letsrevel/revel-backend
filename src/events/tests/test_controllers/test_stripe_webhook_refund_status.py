"""The refund webhooks read the Stripe refund status (#1507).

A refund counts as money returned only when Stripe says ``succeeded``. A ``pending`` refund stays a
PENDING ``Refund`` row. A ``failed`` or ``canceled`` refund marks its row FAILED, also after the
row was SUCCEEDED, and staff get an alert.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationStaff,
    Payment,
    PermissionMap,
    PermissionsSchema,
    Refund,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.models.attendee_invoice import AttendeeInvoiceCreditNote
from events.service.stripe_webhooks import handle_event
from events.tests.test_attendee_invoice._helpers import _create_issued
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

INTENT = "pi_1507"
_FAILURE_REASON = "expired_or_canceled_card"


def _refund(
    refund_id: str = "re_1",
    amount: int = 4000,
    status: str | None = "succeeded",
    *,
    metadata: dict[str, str] | None = None,
    failure_reason: str | None = None,
    intent: str = INTENT,
) -> dict[str, t.Any]:
    """A Stripe refund object. ``status=None`` leaves the key out."""
    refund: dict[str, t.Any] = {
        "object": "refund",
        "id": refund_id,
        "amount": amount,
        "payment_intent": intent,
        "charge": "ch_1507",
        "metadata": metadata or {},
        "failure_reason": failure_reason,
    }
    if status is not None:
        refund["status"] = status
    return refund


def _event(event_type: str, obj: dict[str, t.Any], event_id: str, account: str | None = None) -> stripe.Event:
    payload: dict[str, t.Any] = {"id": event_id, "type": event_type, "livemode": False, "data": {"object": obj}}
    if account is not None:
        payload["account"] = account
    return stripe.Event.construct_from(payload, "sk_test")


def _charge_refunded(refunds: list[dict[str, t.Any]], event_id: str, intent: str = INTENT) -> stripe.Event:
    charge = {"id": "ch_1507", "payment_intent": intent, "refunds": {"data": refunds}}
    return _event("charge.refunded", charge, event_id)


def _refund_event(event_type: str, refund: dict[str, t.Any], event_id: str, account: str | None = None) -> stripe.Event:
    return _event(event_type, refund, event_id, account)


def _deliver(event: stripe.Event, capture: t.Any) -> list[t.MutableMapping[str, t.Any]]:
    """Run the webhook entry point and its on-commit callbacks."""
    with capture_logs() as logs:
        with capture(execute=True):
            handle_event(event)
    return logs


def _lines(logs: list[t.MutableMapping[str, t.Any]], name: str) -> list[t.MutableMapping[str, t.Any]]:
    return [line for line in logs if line["event"] == name]


def _recipients(notification_type: str, exclude: set[t.Any] | None = None) -> list[t.Any]:
    qs = Notification.objects.filter(notification_type=notification_type)
    if exclude:
        qs = qs.exclude(pk__in=exclude)
    return list(qs.values_list("user_id", flat=True))


def _notification_ids() -> set[t.Any]:
    return set(Notification.objects.values_list("pk", flat=True))


@pytest.fixture(autouse=True)
def credit_note_delay() -> t.Iterator[MagicMock]:
    """Never run the credit-note task; tests read its calls."""
    from events.tasks import generate_attendee_credit_note_task

    with patch.object(generate_attendee_credit_note_task, "delay") as delay:
        yield delay


@pytest.fixture
def payment(
    payment_factory: t.Callable[..., Payment],
    ticket_factory: t.Callable[..., Ticket],
    tier_online_with_cancellation_enabled: TicketTier,
) -> Payment:
    ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
    return payment_factory(ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id=INTENT)


@pytest.fixture
def ticket_staff(organization: Organization, django_user_model: t.Type[RevelUser]) -> RevelUser:
    """Staff with the default permissions, which include ``manage_tickets``."""
    user = django_user_model.objects.create_user(username="tickets_staff", email="ts@example.com", password="pass")
    OrganizationStaff.objects.create(organization=organization, user=user)
    return user


@pytest.fixture
def other_staff(organization: Organization, django_user_model: t.Type[RevelUser]) -> RevelUser:
    """Staff without ``manage_tickets``."""
    user = django_user_model.objects.create_user(username="other_staff", email="os@example.com", password="pass")
    OrganizationStaff.objects.create(
        organization=organization,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(manage_tickets=False)).model_dump(mode="json"),
    )
    return user


def _refund_row(
    payment: Payment, status: Refund.RefundStatus, stripe_id: str = "re_1", amount: str = "40.00"
) -> Refund:
    return Refund.objects.create(
        payment=payment,
        amount=Decimal(amount),
        currency=payment.currency,
        status=status,
        source=Refund.Source.ORGANIZER_API,
        stripe_refund_id=stripe_id,
        succeeded_at=timezone.now() if status == Refund.RefundStatus.SUCCEEDED else None,
    )


class TestPendingRefundIsNotMoneyReturned:
    def test_a_pending_refund_keeps_its_pending_row(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        row = _refund_row(payment, Refund.RefundStatus.PENDING)
        Payment.objects.filter(pk=payment.pk).update(refund_status=Payment.RefundStatus.PENDING)

        refund = _refund("re_1", 4000, "pending", metadata={"refund_id": str(row.pk)})
        _deliver(_charge_refunded([refund], "evt_c1"), django_capture_on_commit_callbacks)

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.PENDING
        assert row.succeeded_at is None
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        assert payment.refund_status == Payment.RefundStatus.PENDING
        credit_note_delay.assert_not_called()
        assert not _recipients(NotificationType.TICKET_REFUNDED)

    def test_a_pending_dashboard_refund_is_applied_once_when_it_succeeds(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _deliver(_charge_refunded([_refund("re_dash", 4000, "pending")], "evt_c2"), django_capture_on_commit_callbacks)

        row = Refund.objects.get(payment=payment)
        assert row.status == Refund.RefundStatus.PENDING
        assert row.source == Refund.Source.STRIPE_DASHBOARD
        assert row.stripe_refund_id == "re_dash"
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_status == Payment.RefundStatus.PENDING
        assert payment.refund_amount is None
        credit_note_delay.assert_not_called()

        done = _refund_event("refund.updated", _refund("re_dash", 4000, "succeeded"), "evt_u2")
        _deliver(done, django_capture_on_commit_callbacks)

        assert Refund.objects.filter(payment=payment).count() == 1
        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.SUCCEEDED
        assert row.succeeded_at is not None
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        assert payment.refund_amount == Decimal("40.00")
        assert payment.refund_status == Payment.RefundStatus.SUCCEEDED
        credit_note_delay.assert_called_once()
        assert len(_recipients(NotificationType.TICKET_REFUNDED)) == 1

    def test_a_refund_without_status_is_pending_and_warns(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        logs = _deliver(
            _charge_refunded([_refund("re_nostatus", 4000, None)], "evt_c3"), django_capture_on_commit_callbacks
        )

        row = Refund.objects.get(payment=payment)
        assert row.status == Refund.RefundStatus.PENDING
        assert _lines(logs, "stripe_refund_status_missing")


class TestFailedRefundInChargeRefunded:
    @pytest.mark.parametrize("status", ["failed", "canceled"])
    def test_a_failed_refund_marks_its_pending_row_failed(
        self, payment: Payment, status: str, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        row = _refund_row(payment, Refund.RefundStatus.PENDING)
        Payment.objects.filter(pk=payment.pk).update(refund_status=Payment.RefundStatus.PENDING)

        refund = _refund("re_1", 4000, status, metadata={"refund_id": str(row.pk)}, failure_reason=_FAILURE_REASON)
        logs = _deliver(_charge_refunded([refund], "evt_c4"), django_capture_on_commit_callbacks)

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.FAILED
        assert row.failure_reason == _FAILURE_REASON
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_status == Payment.RefundStatus.FAILED
        assert payment.refund_failure_reason == _FAILURE_REASON
        assert payment.refund_amount is None
        assert len(_lines(logs, "ticket_refund_failed")) == 1


class TestLateFailureOfASucceededRefund:
    def _refund_fully(self, payment: Payment, capture: t.Any) -> Refund:
        _deliver(_charge_refunded([_refund("re_1", 4000, "succeeded")], "evt_first"), capture)
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        return Refund.objects.get(payment=payment)

    def test_a_full_refund_that_fails_later_reopens_the_payment(
        self,
        payment: Payment,
        organization: Organization,
        event: Event,
        organization_owner_user: RevelUser,
        ticket_staff: RevelUser,
        other_staff: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        row = self._refund_fully(payment, django_capture_on_commit_callbacks)
        invoice = _create_issued(organization, event, organization_owner_user)
        credit_note = AttendeeInvoiceCreditNote.objects.create(
            invoice=invoice,
            credit_note_number="CN-1507-1",
            amount_gross=Decimal("40.00"),
            amount_net=Decimal("32.79"),
            amount_vat=Decimal("7.21"),
            issued_at=timezone.now(),
        )
        credit_note.refunds.add(row)
        seen = _notification_ids()

        failed = _refund_event(
            "refund.failed", _refund("re_1", 4000, "failed", failure_reason=_FAILURE_REASON), "evt_f"
        )
        logs = _deliver(failed, django_capture_on_commit_callbacks)

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.FAILED
        assert row.failure_reason == _FAILURE_REASON
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount is None
        assert payment.refund_status == Payment.RefundStatus.FAILED
        assert payment.refund_failure_reason == _FAILURE_REASON
        assert payment.refunded_at is None
        (line,) = _lines(logs, "ticket_refund_failed")
        assert line == {
            "event": "ticket_refund_failed",
            "log_level": "error",
            "payment_id": str(payment.pk),
            "payment_intent_id": INTENT,
            "organization_id": str(organization.pk),
            "event_id": str(event.pk),
            "ticket_id": str(payment.ticket_id),
            "refund_id": "re_1",
            "status": "failed",
            "failure_reason": _FAILURE_REASON,
            "previous_status": "succeeded",
            "credit_note_issued": True,
        }
        # Staff with manage_tickets and the owner are told; staff without the permission are not.
        staff = set(_recipients(NotificationType.REFUND_FAILED, seen))
        assert staff == {organization_owner_user.pk, ticket_staff.pk}
        context = Notification.objects.get(
            notification_type=NotificationType.REFUND_FAILED, user=organization_owner_user
        ).context
        assert context["kind"] == "ticket"
        assert context["credit_note_numbers"] == ["CN-1507-1"]
        assert context["refund_amount"] == "40.00"
        assert context["failure_reason"] == _FAILURE_REASON
        # The buyer got a "refunded" email earlier, so the buyer is told it failed.
        assert _recipients(NotificationType.TICKET_REFUND_FAILED, seen) == [payment.user_id]
        # Moving the payment back never sends a payment confirmation.
        assert not _recipients(NotificationType.PAYMENT_CONFIRMATION, seen)
        credit_note.refresh_from_db()
        assert credit_note.credit_note_number == "CN-1507-1"
        assert AttendeeInvoiceCreditNote.objects.count() == 1

    def test_a_series_pass_ticket_gets_no_buyer_failure_email(
        self,
        payment: Payment,
        event_series: EventSeries,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        series_pass = SeriesPass.objects.create(
            event_series=event_series, name="Pass", price=Decimal("40.00"), pro_rata_discount=Decimal("0.00")
        )
        ticket = payment.ticket
        ticket.held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=payment.user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=Decimal("40.00"),
        )
        ticket.save(update_fields=["held_pass"])
        self._refund_fully(payment, django_capture_on_commit_callbacks)
        seen = _notification_ids()

        failed = _refund_event(
            "refund.failed", _refund("re_1", 4000, "failed", failure_reason=_FAILURE_REASON), "evt_sp"
        )
        _deliver(failed, django_capture_on_commit_callbacks)

        assert not _recipients(NotificationType.TICKET_REFUND_FAILED, seen)
        assert organization_owner_user.pk in _recipients(NotificationType.REFUND_FAILED, seen)

    def test_a_partial_refund_that_fails_later_keeps_the_other_refund(
        self,
        payment: Payment,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _deliver(_charge_refunded([_refund("re_a", 1000, "succeeded")], "evt_a"), django_capture_on_commit_callbacks)
        _deliver(_charge_refunded([_refund("re_b", 1500, "succeeded")], "evt_b"), django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.refund_amount == Decimal("25.00")
        seen = _notification_ids()

        failed = _refund_event(
            "refund.failed", _refund("re_b", 1500, "failed", failure_reason=_FAILURE_REASON), "evt_fb"
        )
        logs = _deliver(failed, django_capture_on_commit_callbacks)

        assert Refund.objects.get(stripe_refund_id="re_b").status == Refund.RefundStatus.FAILED
        assert Refund.objects.get(stripe_refund_id="re_a").status == Refund.RefundStatus.SUCCEEDED
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount == Decimal("10.00")
        assert payment.refund_status == Payment.RefundStatus.SUCCEEDED
        (line,) = _lines(logs, "ticket_refund_failed")
        assert line["previous_status"] == "succeeded"
        assert line["refund_id"] == "re_b"
        assert _recipients(NotificationType.REFUND_FAILED, seen) == [organization_owner_user.pk]
        # The payment was never fully refunded, so no "refunded" email went out: the buyer hears nothing.
        assert not _recipients(NotificationType.TICKET_REFUND_FAILED, seen)

    def test_a_batch_refund_that_fails_later_reopens_every_payment(
        self,
        batch_of_4_online_payments: list[Payment],
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        payments = batch_of_4_online_payments
        Payment.objects.filter(pk__in=[p.pk for p in payments]).update(stripe_payment_intent_id="pi_batch")
        refund = _refund("re_batch", 16000, "succeeded", intent="pi_batch")
        _deliver(_charge_refunded([refund], "evt_b1", intent="pi_batch"), django_capture_on_commit_callbacks)
        assert Refund.objects.filter(stripe_refund_id="re_batch").count() == 4
        seen = _notification_ids()

        failed = _refund_event(
            "refund.failed",
            _refund("re_batch", 16000, "failed", intent="pi_batch", failure_reason="lost_or_stolen_card"),
            "evt_b2",
        )
        logs = _deliver(failed, django_capture_on_commit_callbacks)

        assert (
            not Refund.objects.filter(stripe_refund_id="re_batch").exclude(status=Refund.RefundStatus.FAILED).exists()
        )
        for p in payments:
            p.refresh_from_db()
            assert p.status == Payment.PaymentStatus.SUCCEEDED
            assert p.refund_status == Payment.RefundStatus.FAILED
        assert len(_lines(logs, "ticket_refund_failed")) == 4
        # One Stripe refund failed: staff get one message. Each payment had its own "refunded" email.
        assert _recipients(NotificationType.REFUND_FAILED, seen) == [organization_owner_user.pk]
        assert len(_recipients(NotificationType.TICKET_REFUND_FAILED, seen)) == 4


class TestEventOrderAndRedelivery:
    def test_refund_updated_before_charge_refunded_applies_the_refund_once(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        updated = _refund_event("refund.updated", _refund("re_dash", 4000, "succeeded"), "evt_u")
        _deliver(updated, django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED

        _deliver(_charge_refunded([_refund("re_dash", 4000, "succeeded")], "evt_c"), django_capture_on_commit_callbacks)

        assert Refund.objects.filter(payment=payment).count() == 1
        assert len(_recipients(NotificationType.TICKET_REFUNDED)) == 1

    def test_refund_failed_and_refund_updated_log_one_failure(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _refund_row(payment, Refund.RefundStatus.SUCCEEDED)
        failed = _refund("re_1", 4000, "failed", failure_reason=_FAILURE_REASON)

        first = _deliver(_refund_event("refund.failed", failed, "evt_1"), django_capture_on_commit_callbacks)
        second = _deliver(_refund_event("refund.updated", failed, "evt_2"), django_capture_on_commit_callbacks)

        assert len(_lines(first, "ticket_refund_failed")) == 1
        assert not _lines(second, "ticket_refund_failed")

    def test_a_redelivered_failure_event_alerts_once(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _refund_row(payment, Refund.RefundStatus.SUCCEEDED)
        failed = _refund_event("refund.failed", _refund("re_1", 4000, "failed"), "evt_same")

        first = _deliver(failed, django_capture_on_commit_callbacks)
        second = _deliver(failed, django_capture_on_commit_callbacks)

        assert len(_lines(first, "ticket_refund_failed")) == 1
        assert not _lines(second, "ticket_refund_failed")
        assert len(_recipients(NotificationType.REFUND_FAILED)) == 1

    def test_a_stale_succeeded_refund_does_not_undo_a_failure(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        row = _refund_row(payment, Refund.RefundStatus.SUCCEEDED)
        _deliver(
            _refund_event("refund.failed", _refund("re_1", 4000, "failed"), "evt_f"), django_capture_on_commit_callbacks
        )
        credit_note_delay.reset_mock()

        _deliver(
            _charge_refunded([_refund("re_1", 4000, "succeeded")], "evt_stale"), django_capture_on_commit_callbacks
        )

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.FAILED
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_status == Payment.RefundStatus.FAILED
        credit_note_delay.assert_not_called()

    def test_a_failure_on_a_connected_account_event_is_applied(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        row = _refund_row(payment, Refund.RefundStatus.SUCCEEDED)
        failed = _refund_event("refund.failed", _refund("re_1", 4000, "failed"), "evt_acct", account="acct_conn_1507")

        logs = _deliver(failed, django_capture_on_commit_callbacks)

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.FAILED
        assert len(_lines(logs, "ticket_refund_failed")) == 1


class TestNegatives:
    def test_a_failure_for_an_unknown_intent_does_nothing(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        row = _refund_row(payment, Refund.RefundStatus.SUCCEEDED, "re_x")
        failed = _refund_event("refund.failed", _refund("re_x", 4000, "failed", intent="pi_unknown"), "evt_u1")

        logs = _deliver(failed, django_capture_on_commit_callbacks)

        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.SUCCEEDED
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert not _lines(logs, "ticket_refund_failed")
        assert not Notification.objects.filter(notification_type=NotificationType.REFUND_FAILED).exists()

    def test_a_failure_for_an_unknown_refund_changes_nothing(
        self, payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        failed = _refund_event("refund.failed", _refund("re_unknown", 4000, "failed"), "evt_u2")

        logs = _deliver(failed, django_capture_on_commit_callbacks)

        assert not Refund.objects.filter(payment=payment).exists()
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_status is None
        assert not _lines(logs, "ticket_refund_failed")
        assert _lines(logs, "stripe_refund_failed_unknown")

    def test_an_ambiguous_refund_update_neither_applies_nor_notifies(
        self, batch_of_4_online_payments: list[Payment], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        Payment.objects.filter(pk__in=[p.pk for p in batch_of_4_online_payments]).update(
            stripe_payment_intent_id="pi_amb"
        )
        updated = _refund_event("refund.updated", _refund("re_amb", 4000, "succeeded", intent="pi_amb"), "evt_amb")

        _deliver(updated, django_capture_on_commit_callbacks)

        assert not Refund.objects.exists()
        assert not Notification.objects.filter(notification_type=NotificationType.REFUND_UNMATCHED).exists()

    def test_a_refund_row_of_another_intent_is_not_touched(
        self,
        payment: Payment,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        other = payment_factory(
            ticket=ticket_factory(tier=tier_online_with_cancellation_enabled),
            stripe_payment_intent_id="pi_other_1507",
        )
        other_row = _refund_row(other, Refund.RefundStatus.SUCCEEDED, stripe_id="re_1")

        failed = _refund_event("refund.failed", _refund("re_1", 4000, "failed"), "evt_other")
        _deliver(failed, django_capture_on_commit_callbacks)

        other_row.refresh_from_db()
        assert other_row.status == Refund.RefundStatus.SUCCEEDED


def test_the_refund_pointer_in_metadata_is_matched_by_primary_key(payment: Payment) -> None:
    """A pointer that is not a UUID is ignored (the refund falls back to the one Payment), not a 500."""
    refund = _refund("re_ptr", 4000, "pending", metadata={"refund_id": "not-a-uuid"})
    handle_event(_charge_refunded([refund], "evt_ptr"))
    assert Refund.objects.get(payment=payment).status == Refund.RefundStatus.PENDING


class TestRefundBatchOrder:
    def test_a_new_refund_is_matched_after_a_failed_refund_reopens_the_payment(
        self,
        payment: Payment,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """R1 refunded the payment, R1 failed, staff refunded again (R2): R2 arrives before R1's failure event."""
        _deliver(_charge_refunded([_refund("re_1", 4000, "succeeded")], "evt_r1"), django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        seen = _notification_ids()

        batch = _charge_refunded(
            [_refund("re_2", 4000, "succeeded"), _refund("re_1", 4000, "failed", failure_reason=_FAILURE_REASON)],
            "evt_r2",
        )
        _deliver(batch, django_capture_on_commit_callbacks)

        assert Refund.objects.get(stripe_refund_id="re_1").status == Refund.RefundStatus.FAILED
        assert Refund.objects.get(stripe_refund_id="re_2").status == Refund.RefundStatus.SUCCEEDED
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        assert payment.refund_amount == Decimal("40.00")
        # The buyer ends the batch refunded: no failure email. Staff still hear about R1.
        assert not _recipients(NotificationType.TICKET_REFUND_FAILED, seen)
        assert organization_owner_user.pk in _recipients(NotificationType.REFUND_FAILED, seen)

    def test_a_payment_settled_in_both_passes_is_reported_once(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """One event settles a PENDING row (pass 1) and carries a new partial refund (pass 2)."""
        _refund_row(payment, Refund.RefundStatus.PENDING, "re_old", "10.00")
        batch = _charge_refunded([_refund("re_old", 1000, "succeeded"), _refund("re_new", 1000, "succeeded")], "evt_d3")
        logs = _deliver(batch, django_capture_on_commit_callbacks)

        assert Refund.objects.filter(payment=payment, status=Refund.RefundStatus.SUCCEEDED).count() == 2
        (line,) = _lines(logs, "stripe_refund_processed")
        assert line["newly_refunded_payment_ids"] == [str(payment.id)]


class TestCreditNoteRetryWithPendingRefund:
    def test_a_redelivery_retries_the_credit_note_of_the_succeeded_partial_refund(
        self, payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        first = _refund_row(payment, Refund.RefundStatus.SUCCEEDED, "re_1", "10.00")
        _refund_row(payment, Refund.RefundStatus.PENDING, "re_2", "30.00")
        payment.refund_status = Payment.RefundStatus.PENDING
        payment.refund_amount = Decimal("10.00")
        payment.save(update_fields=["refund_status", "refund_amount"])

        redelivery = _charge_refunded([_refund("re_1", 1000, "succeeded"), _refund("re_2", 3000, "pending")], "evt_cn")
        _deliver(redelivery, django_capture_on_commit_callbacks)

        credit_note_delay.assert_called_once_with(payment.stripe_session_id, [str(payment.id)], [str(first.pk)])


class TestOneBuyerCorrectionPerPayment:
    def test_two_refunds_of_one_payment_that_fail_together_send_the_buyer_one_message(
        self,
        payment: Payment,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A 40.00 payment was refunded by 10.00 and 30.00; both fail in one delivery."""
        both = _charge_refunded([_refund("re_a", 1000, "succeeded"), _refund("re_b", 3000, "succeeded")], "evt_two")
        _deliver(both, django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        seen = _notification_ids()

        failed = _charge_refunded(
            [_refund("re_a", 1000, "failed"), _refund("re_b", 3000, "failed", failure_reason=_FAILURE_REASON)],
            "evt_two_failed",
        )
        _deliver(failed, django_capture_on_commit_callbacks)

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        (buyer_notification,) = Notification.objects.filter(
            notification_type=NotificationType.TICKET_REFUND_FAILED
        ).exclude(pk__in=seen)
        assert buyer_notification.user_id == payment.user_id
        assert buyer_notification.context["refund_amount"] == f"40.00 {payment.currency}"
        # Staff are told once for each Stripe refund.
        assert len(_recipients(NotificationType.REFUND_FAILED, seen)) == 2


class TestRedeliveryRetriesTheCreditNote:
    @pytest.mark.parametrize("event_type", ["refund.updated", "refund.failed"])
    def test_a_redelivered_refund_event_retries_the_credit_note_of_the_succeeded_refund(
        self,
        event_type: str,
        payment: Payment,
        credit_note_delay: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The first delivery is stored; the same event id again goes to ``replay()``, not to the handler."""
        succeeded = _refund_row(payment, Refund.RefundStatus.SUCCEEDED, "re_1", "10.00")
        _refund_row(payment, Refund.RefundStatus.PENDING, "re_2", "30.00")
        refund = _refund("re_2", 3000, "pending")
        event = _refund_event(event_type, refund, "evt_replay")
        _deliver(event, django_capture_on_commit_callbacks)
        credit_note_delay.reset_mock()

        _deliver(event, django_capture_on_commit_callbacks)

        credit_note_delay.assert_called_once_with(payment.stripe_session_id, [str(payment.id)], [str(succeeded.pk)])


def test_the_replay_passes_payments_and_refund_rows_that_agree(
    payment: Payment, credit_note_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A refund.failed that commits between two reads must not leave a payment id with no refund id.

    The credit-note task reads an empty refund list as "the full payment amount".
    """
    from django.db import connection

    row = _refund_row(payment, Refund.RefundStatus.SUCCEEDED, "re_1", "40.00")
    event = _refund_event("refund.updated", _refund("re_1", 4000, "succeeded"), "evt_race")
    _deliver(event, django_capture_on_commit_callbacks)
    credit_note_delay.reset_mock()
    fired: list[bool] = []

    def commit_a_failure_after_the_first_read(
        execute: t.Callable[..., t.Any], sql: str, params: t.Any, many: bool, context: t.Any
    ) -> t.Any:
        result = execute(sql, params, many, context)
        if not fired and sql.startswith("SELECT") and "events_refund" in sql:
            fired.append(True)
            Refund.objects.filter(pk=row.pk).update(status=Refund.RefundStatus.FAILED)
        return result

    with connection.execute_wrapper(commit_a_failure_after_the_first_read):
        _deliver(event, django_capture_on_commit_callbacks)

    ((_, ids, refund_ids),) = [call.args for call in credit_note_delay.call_args_list]
    assert fired, "the injected concurrent refund change did not run"
    assert bool(ids) == bool(refund_ids)
