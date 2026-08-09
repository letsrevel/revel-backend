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
    charge_object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "refunds": {"data": refunds},
    }
    payload = {
        "id": event_id,
        "type": "charge.refunded",
        "account": None,
        "livemode": False,
        "data": {"object": charge_object},
    }
    ev = MagicMock(spec=stripe.Event)
    ev.id = event_id
    ev.type = "charge.refunded"
    ev.account = None
    ev.livemode = False
    ev.data = MagicMock()
    ev.data.object = charge_object
    # dict(event) goes through the mapping protocol, not __iter__: stripe.Event
    # exposes `.keys`, and MagicMock(spec=...) auto-mocks that attribute too, so
    # dict() calls ev.keys() + ev[key] rather than iterating ev directly. Both
    # must be wired or dict(event) silently returns {} regardless of __iter__.
    ev.keys.return_value = list(payload.keys())
    ev.__getitem__.side_effect = payload.__getitem__
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
        assert payment.raw_response["id"] == "evt_refund", "the webhook event must be persisted, not a stub"
        assert payment.raw_response["type"] == "charge.refunded"

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
        first_event = _charge_event("pi_dashboard_accum", [first])
        StripeEventHandler(first_event).handle_charge_refunded(first_event)

        second: dict[str, t.Any] = {"id": "re_2", "amount": 1500, "metadata": {}}
        second_event = _charge_event("pi_dashboard_accum", [second])
        StripeEventHandler(second_event).handle_charge_refunded(second_event)

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
        # The confirmation stamp drives revenue-report period attribution.
        assert pending_row.succeeded_at == payment.refunded_at
        assert pending_row.succeeded_at is not None

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

    def test_metadata_refund_id_discriminates_among_batch_candidates(
        self,
        batch_of_4_online_payments: list[Payment],
    ) -> None:
        """Branch 1.5 must resolve to the exact candidate the metadata names.

        No Payment in this batch carries the legacy ``stripe_refund_id`` mirror,
        and all four cost the same amount, so Branches 1, 2, 2.5, and 3 cannot
        resolve this: 3's uniform-amount match is ambiguous across all four rows.
        Only the metadata ``refund_id`` pointer (Branch 1.5) picks out candidate
        #3 specifically — proving the branch is load-bearing, not merely present.
        """
        payments = batch_of_4_online_payments
        Payment.objects.filter(pk__in=[p.pk for p in payments]).update(
            stripe_payment_intent_id="pi_batch_meta", status=Payment.PaymentStatus.SUCCEEDED
        )
        target = payments[2]
        pending_row = Refund.objects.create(
            payment=target,
            amount=target.amount,
            currency=target.currency,
            status=Refund.RefundStatus.PENDING,
            source=Refund.Source.USER_CANCELLATION,
        )

        refund: dict[str, t.Any] = {
            "id": "re_meta_disc",
            "amount": int(target.amount * 100),
            "metadata": {"refund_id": str(pending_row.pk)},
        }
        event = _charge_event("pi_batch_meta", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        pending_row.refresh_from_db()
        assert pending_row.status == Refund.RefundStatus.SUCCEEDED
        assert pending_row.stripe_refund_id == "re_meta_disc"
        target.refresh_from_db()
        assert target.status == Payment.PaymentStatus.REFUNDED

        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED, "untouched candidates must not be refunded"
            assert not Refund.objects.filter(payment=other).exists()

    def test_replay_after_mirror_overwritten_resolves_via_refund_row(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        """A later refund can overwrite ``Payment.stripe_refund_id``; a replay of an
        earlier refund must still resolve via its own ``Refund`` row (the DB leg of
        Branch 1), not fail to match or fall through to a fresh Branch 2.5 match.
        """
        tier = tier_online_with_cancellation_enabled
        payment = _single_payment(payment_factory, ticket_factory, tier, "pi_mirror_overwritten")

        Refund.objects.create(
            payment=payment,
            amount=Decimal("10.00"),
            currency=payment.currency,
            status=Refund.RefundStatus.SUCCEEDED,
            stripe_refund_id="re_1",
            source=Refund.Source.STRIPE_DASHBOARD,
        )
        # A second refund landed after the first and overwrote the legacy mirror.
        Payment.objects.filter(pk=payment.pk).update(
            stripe_refund_id="re_2", refund_amount=Decimal("10.00"), refund_status=Payment.RefundStatus.SUCCEEDED
        )
        payment.refresh_from_db()

        refund: dict[str, t.Any] = {"id": "re_1", "amount": 1000, "metadata": {}}
        event = _charge_event("pi_mirror_overwritten", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        assert Refund.objects.filter(payment=payment).count() == 1, "must not create a duplicate row for a replay"
        payment.refresh_from_db()
        assert payment.refund_amount == Decimal("10.00"), "totals must not change on a replay"
