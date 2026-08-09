"""Tests for the record-only `charge.refunded` webhook (#865).

A refund never cancels a ticket and never reclaims tier capacity — cancel and
refund are orthogonal operations. The webhook only finalizes ``Refund`` rows
and maintains ``Payment``'s denormalized refund totals.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe

from events.models import Payment, Refund, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler, handle_event

pytestmark = pytest.mark.django_db


def _charge_event(
    payment_intent_id: str,
    refunds: list[dict[str, t.Any]],
    event_id: str = "evt_refund",
) -> stripe.Event:
    ev = MagicMock(spec=stripe.Event)
    ev.id = event_id
    ev.type = "charge.refunded"
    ev.account = None
    ev.livemode = False
    ev.data = MagicMock()
    ev.data.object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "refunds": {"data": refunds},
    }
    ev.__iter__.return_value = iter([])
    return ev


def _single_payment(
    payment_factory: t.Callable[..., Payment],
    ticket_factory: t.Callable[..., Ticket],
    tier: TicketTier,
    intent_id: str,
    amount: Decimal = Decimal("40.00"),
) -> Payment:
    ticket = ticket_factory(tier=tier)
    payment = payment_factory(ticket=ticket, amount=amount)
    Payment.objects.filter(pk=payment.pk).update(
        stripe_payment_intent_id=intent_id, status=Payment.PaymentStatus.SUCCEEDED
    )
    payment.refresh_from_db()
    return payment


class TestRecordOnlyRefunds:
    def test_dashboard_refund_marks_payment_but_ticket_stays_active(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=1)
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_dashboard_full")

        refund: dict[str, t.Any] = {"id": "re_full", "amount": 4000, "metadata": {}}
        event = _charge_event("pi_dashboard_full", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        row = Refund.objects.get(payment=payment)
        assert row.source == Refund.Source.STRIPE_DASHBOARD
        assert row.status == Refund.RefundStatus.SUCCEEDED
        assert row.stripe_refund_id == "re_full"
        assert row.amount == Decimal("40.00")

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        assert payment.refund_amount == Decimal("40.00")

        payment.ticket.refresh_from_db()
        assert payment.ticket.status == Ticket.TicketStatus.ACTIVE
        assert payment.ticket.cancellation_source == ""

        tier.refresh_from_db()
        assert tier.quantity_sold == 1

    def test_partial_dashboard_refund_leaves_payment_succeeded(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_dashboard_partial")

        refund: dict[str, t.Any] = {"id": "re_partial", "amount": 1000, "metadata": {}}
        event = _charge_event("pi_dashboard_partial", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        row = Refund.objects.get(payment=payment)
        assert row.amount == Decimal("10.00")

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_amount == Decimal("10.00")

        payment.ticket.refresh_from_db()
        assert payment.ticket.status == Ticket.TicketStatus.ACTIVE

    def test_second_partial_refund_accumulates(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_dashboard_accum")

        first: dict[str, t.Any] = {"id": "re_1", "amount": 1000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_dashboard_accum", [first])).handle_charge_refunded(
            _charge_event("pi_dashboard_accum", [first])
        )
        second: dict[str, t.Any] = {"id": "re_2", "amount": 1500, "metadata": {}}
        StripeEventHandler(_charge_event("pi_dashboard_accum", [second])).handle_charge_refunded(
            _charge_event("pi_dashboard_accum", [second])
        )

        assert Refund.objects.filter(payment=payment).count() == 2
        payment.refresh_from_db()
        assert payment.refund_amount == Decimal("25.00")
        assert payment.status == Payment.PaymentStatus.SUCCEEDED

    def test_app_initiated_refund_finalizes_pending_row_via_metadata(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_app_initiated")
        pending_row = Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency=payment.currency,
            status=Refund.RefundStatus.PENDING,
            source=Refund.Source.USER_CANCELLATION,
        )

        refund: dict[str, t.Any] = {
            "id": "re_app",
            "amount": 4000,
            "metadata": {"refund_id": str(pending_row.pk)},
        }
        event = _charge_event("pi_app_initiated", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        assert Refund.objects.filter(payment=payment).count() == 1
        pending_row.refresh_from_db()
        assert pending_row.status == Refund.RefundStatus.SUCCEEDED
        assert pending_row.stripe_refund_id == "re_app"

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        assert payment.refund_amount == Decimal("40.00")

    def test_replay_is_idempotent(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_replay")

        refund: dict[str, t.Any] = {"id": "re_replay", "amount": 1000, "metadata": {}}
        handle_event(_charge_event("pi_replay", [refund], event_id="evt_1"))
        handle_event(_charge_event("pi_replay", [refund], event_id="evt_2"))

        assert Refund.objects.filter(payment=payment).count() == 1
        payment.refresh_from_db()
        assert payment.refund_amount == Decimal("10.00")
