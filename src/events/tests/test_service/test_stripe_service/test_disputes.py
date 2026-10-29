"""Tests for the ``charge.dispute.*`` webhook handler (#1245)."""

import typing as t
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from accounts.models import RevelUser
from events.management.commands.provision_stripe_webhooks import CONNECT_EVENTS, PLATFORM_EVENTS
from events.models import Dispute, Event, Organization, Payment, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db

DUE_BY = 1_800_000_000


def _dispute_object(
    *,
    dispute_id: str = "dp_test1",
    status: str = "needs_response",
    payment_intent: str | None = "pi_dispute1",
    amount: int = 5000,
) -> dict[str, t.Any]:
    return {
        "id": dispute_id,
        "object": "dispute",
        "amount": amount,
        "currency": "eur",
        "charge": "ch_dispute1",
        "payment_intent": payment_intent,
        "reason": "fraudulent",
        "status": status,
        "evidence_details": {"due_by": DUE_BY, "has_evidence": False, "past_due": False, "submission_count": 0},
    }


def _handle(event_type: str, obj: dict[str, t.Any], account: str | None = None, event_id: str = "evt_1") -> None:
    event = MagicMock(spec=stripe.Event)
    event.id = event_id
    event.type = event_type
    event.account = account
    event.data = MagicMock()
    event.data.object = obj
    assert StripeEventHandler(event).handle() is True


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_dispute1"
    organization.save()
    return organization


def _payment(event: Event, user: RevelUser, intent: str = "pi_dispute1", n: int = 1) -> list[Payment]:
    tier = TicketTier.objects.create(event=event, name="Paid", price=Decimal("25.00"), currency="EUR")
    payments = []
    for i in range(n):
        ticket = Ticket.objects.create(
            event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name=f"Guest {i}"
        )
        payments.append(
            Payment.objects.create(
                ticket=ticket,
                user=user,
                stripe_session_id="cs_dispute1",
                stripe_payment_intent_id=intent,
                amount=Decimal("25.00"),
                platform_fee=Decimal("0.00"),
                currency="EUR",
                status=Payment.PaymentStatus.SUCCEEDED,
                raw_response={},
            )
        )
    return payments


def _sent_types(send_mock: MagicMock) -> list[str]:
    return [c.kwargs["notification_type"] for c in send_mock.call_args_list]


NOTIFY = "notifications.signals.payment.notification_requested.send"


class TestUpsert:
    def test_created_records_dispute(self, stripe_org: Organization) -> None:
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1")

        dispute = Dispute.objects.get(stripe_dispute_id="dp_test1")
        assert dispute.organization == stripe_org
        assert dispute.amount == Decimal("50")
        assert dispute.currency == "EUR"
        assert dispute.reason == "fraudulent"
        assert dispute.status == Dispute.DisputeStatus.NEEDS_RESPONSE
        assert dispute.stripe_charge_id == "ch_dispute1"
        assert dispute.stripe_payment_intent_id == "pi_dispute1"
        assert dispute.evidence_due_by == datetime.fromtimestamp(DUE_BY, tz=UTC)
        assert dispute.closed_at is None

    def test_updated_then_closed_transitions(self, stripe_org: Organization) -> None:
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1", event_id="evt_a")
            _handle(
                "charge.dispute.updated",
                _dispute_object(status="under_review"),
                account="acct_dispute1",
                event_id="evt_b",
            )
            assert Dispute.objects.get().status == Dispute.DisputeStatus.UNDER_REVIEW
            assert Dispute.objects.get().closed_at is None
            _handle("charge.dispute.closed", _dispute_object(status="lost"), account="acct_dispute1", event_id="evt_c")

        dispute = Dispute.objects.get()
        assert Dispute.objects.count() == 1
        assert dispute.status == Dispute.DisputeStatus.LOST
        assert dispute.closed_at is not None

    def test_redelivery_is_idempotent(self, stripe_org: Organization) -> None:
        with patch(NOTIFY):
            _handle("charge.dispute.closed", _dispute_object(status="won"), account="acct_dispute1")
            first_closed_at = Dispute.objects.get().closed_at
            _handle("charge.dispute.closed", _dispute_object(status="won"), account="acct_dispute1")

        assert Dispute.objects.count() == 1
        assert Dispute.objects.get().closed_at == first_closed_at

    def test_closed_before_created_keeps_terminal_status(self, stripe_org: Organization) -> None:
        with patch(NOTIFY) as send_mock:
            _handle("charge.dispute.closed", _dispute_object(status="won"), account="acct_dispute1", event_id="evt_c")
            _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1", event_id="evt_a")

        dispute = Dispute.objects.get()
        assert dispute.status == Dispute.DisputeStatus.WON
        assert dispute.closed_at is not None
        # Only the closed notification: the stale "opened" alert would point at a deadline that no longer matters.
        assert _sent_types(send_mock) == [NotificationType.STAFF_PAYMENT_DISPUTED]
        assert send_mock.call_args.kwargs["context"]["outcome"] == "closed"


class TestOrganizationResolution:
    def test_via_connected_account(self, stripe_org: Organization) -> None:
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(payment_intent=None), account="acct_dispute1")
        assert Dispute.objects.get().organization == stripe_org

    def test_via_payment_fallback(
        self, event: Event, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        _payment(event, organization_owner_user)
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(), account=None)
        assert Dispute.objects.get().organization == organization

    def test_unmatched_is_recorded_without_organization(self) -> None:
        with patch(NOTIFY) as send_mock:
            _handle("charge.dispute.created", _dispute_object(), account="acct_unknown")

        dispute = Dispute.objects.get()
        assert dispute.organization is None
        assert not send_mock.called

    def test_unmatched_row_gets_organization_on_later_event(
        self, event: Event, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(), account=None)
            assert Dispute.objects.get().organization is None
            _payment(event, organization_owner_user)
            _handle("charge.dispute.updated", _dispute_object(status="under_review"), account=None)
        assert Dispute.objects.get().organization == organization

    def test_multi_payment_intent_links_all_payments(
        self, event: Event, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        payments = _payment(event, organization_owner_user, n=3)
        with patch(NOTIFY):
            _handle("charge.dispute.created", _dispute_object(), account=None)

        dispute = Dispute.objects.get()
        assert dispute.organization == organization
        assert set(dispute.linked_payments()) == set(payments)


class TestNotifications:
    def test_created_and_closed_notify_but_updated_does_not(self, stripe_org: Organization) -> None:
        with patch(NOTIFY) as send_mock:
            _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1", event_id="evt_a")
            assert _sent_types(send_mock) == [NotificationType.STAFF_PAYMENT_DISPUTED]
            opened = send_mock.call_args.kwargs
            assert opened["user"] == stripe_org.owner
            assert opened["context"]["outcome"] == "opened"
            assert opened["context"]["amount"] == "50.00"
            assert opened["context"]["reason"] == "fraudulent"
            assert opened["context"]["evidence_due_by"] == "2027-01-15 08:00 UTC"
            assert "dp_test1" in opened["context"]["dashboard_url"]
            assert "email" not in " ".join(str(v) for v in opened["context"].values()).lower()

            send_mock.reset_mock()
            _handle(
                "charge.dispute.updated",
                _dispute_object(status="under_review"),
                account="acct_dispute1",
                event_id="evt_b",
            )
            assert not send_mock.called

            _handle("charge.dispute.closed", _dispute_object(status="lost"), account="acct_dispute1", event_id="evt_c")
            assert _sent_types(send_mock) == [NotificationType.STAFF_PAYMENT_DISPUTED]
            closed = send_mock.call_args.kwargs["context"]
            assert closed["outcome"] == "closed"
            assert closed["status"] == "lost"

    def test_context_names_event_without_buyer_identity(
        self, event: Event, stripe_org: Organization, organization_owner_user: RevelUser
    ) -> None:
        _payment(event, organization_owner_user)
        with patch(NOTIFY) as send_mock:
            _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1")

        context = send_mock.call_args.kwargs["context"]
        assert context["event_name"] == event.name
        assert organization_owner_user.email not in str(context)


def test_dispute_is_record_only(event: Event, stripe_org: Organization, organization_owner_user: RevelUser) -> None:
    payment = _payment(event, organization_owner_user)[0]
    ticket_status = payment.ticket.status
    with patch(NOTIFY):
        _handle("charge.dispute.created", _dispute_object(), account="acct_dispute1", event_id="evt_a")
        _handle("charge.dispute.closed", _dispute_object(status="lost"), account="acct_dispute1", event_id="evt_c")

    payment.refresh_from_db()
    payment.ticket.refresh_from_db()
    assert payment.status == Payment.PaymentStatus.SUCCEEDED
    assert payment.refund_status != Payment.RefundStatus.SUCCEEDED
    assert payment.ticket.status == ticket_status


def test_provisioning_subscribes_dispute_events() -> None:
    expected = {"charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed"}
    assert expected <= set(PLATFORM_EVENTS)
    assert expected <= set(CONNECT_EVENTS)
