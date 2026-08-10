"""Tests for handle_charge_refunded's per-payment matching strategy."""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from events.models import Payment, Refund, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


def _charge_event(
    payment_intent_id: str,
    refunds: list[dict[str, t.Any]] | None,
    account: str | None = None,
) -> stripe.Event:
    ev = MagicMock(spec=stripe.Event)
    ev.type = "charge.refunded"
    ev.account = account
    ev.data = MagicMock()
    obj: dict[str, t.Any] = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
    }
    # refunds=None mimics a payload rendered at API >= 2022-11-15 (pinned
    # endpoint): the refunds list is not embedded at all.
    if refunds is not None:
        obj["refunds"] = {"data": refunds}
    ev.data.object = obj
    # Make dict(event) serializable — tests shouldn't care about exact shape,
    # so patch out raw_response assignment in the handler by providing __iter__.
    ev.__iter__.return_value = iter([])
    return ev


def _batch(payments: list[Payment], intent_id: str) -> None:
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(
        stripe_payment_intent_id=intent_id, status=Payment.PaymentStatus.SUCCEEDED
    )


class TestChargeRefundedMatching:
    def test_branch_1_existing_stripe_refund_id_match(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[1]
        target.stripe_refund_id = "re_already_recorded"
        target.refund_status = Payment.RefundStatus.PENDING
        target.save(update_fields=["stripe_refund_id", "refund_status"])

        refund: dict[str, t.Any] = {"id": "re_already_recorded", "amount": 4000, "metadata": {}}
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        assert target.status == Payment.PaymentStatus.REFUNDED
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED, "other payments must be untouched"

    def test_branch_2_metadata_ticket_id_match(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[2]
        refund: dict[str, t.Any] = {
            "id": "re_new",
            "amount": 4000,
            "metadata": {"ticket_id": str(target.ticket_id)},
        }
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)
        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        target.ticket.refresh_from_db()
        assert target.ticket.status == Ticket.TicketStatus.ACTIVE, "refunds are record-only; the ticket stays active"
        assert target.ticket.cancellation_source == ""
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED

    def test_branch_3_exact_amount_unambiguous(self, batch_of_4_online_payments: list[Payment]) -> None:
        """Uniform batch, one unrefunded row left: the amount match is the only reading."""
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        # Three of the four are already refunded, so only one row can match.
        Payment.objects.filter(pk__in=[p.pk for p in payments[1:]]).update(
            refund_status=Payment.RefundStatus.SUCCEEDED, status=Payment.PaymentStatus.REFUNDED
        )

        refund: dict[str, t.Any] = {"id": "re_new", "amount": 4000, "metadata": {}}
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)
        payments[0].refresh_from_db()
        assert payments[0].refund_status == Payment.RefundStatus.SUCCEEDED

    def test_branch_4_full_intent_refund_applies_to_all(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        tier_id = payments[0].ticket.tier_id
        TicketTier.objects.filter(pk=tier_id).update(quantity_sold=4)
        total_cents = int(sum(p.amount for p in payments) * 100)
        refund: dict[str, t.Any] = {"id": "re_full", "amount": total_cents, "metadata": {}}
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)
        for p in payments:
            p.refresh_from_db()
            assert p.refund_status == Payment.RefundStatus.SUCCEEDED
            assert p.status == Payment.PaymentStatus.REFUNDED
            assert p.refund_amount == p.amount, "Branch 4 must allocate per-Payment, not the total"
            p.ticket.refresh_from_db()
            assert p.ticket.status == Ticket.TicketStatus.ACTIVE, "refunds are record-only; tickets stay active"
            tier = TicketTier.objects.get(pk=p.ticket.tier_id)
            assert tier.quantity_sold == 4, "no seat may be reclaimed by a refund"

    def test_branch_5_ambiguous_logged_no_mutation(
        self, batch_of_4_online_payments: list[Payment], caplog: pytest.LogCaptureFixture
    ) -> None:
        payments = batch_of_4_online_payments
        # All payments cost the same AND amount doesn't equal full intent.
        _batch(payments, "pi_batch")
        refund: dict[str, t.Any] = {"id": "re_ambig", "amount": 4000, "metadata": {}}  # matches any single payment
        event = _charge_event("pi_batch", [refund])
        with caplog.at_level("WARNING", logger="events.service.stripe_webhook_refunds"):
            StripeEventHandler(event).handle_charge_refunded(event)
        for p in payments:
            p.refresh_from_db()
            assert p.status == Payment.PaymentStatus.SUCCEEDED
            assert p.refund_status is None
        # The whole point of branch 5 is that the ambiguity is logged loudly
        # so a future regression that swallows the warning gets caught here.
        assert any("stripe_refund_ambiguous_match" in record.message for record in caplog.records)

    def test_duplicate_webhook_is_idempotent(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[0]
        target.stripe_refund_id = "re_a"
        target.refund_status = Payment.RefundStatus.SUCCEEDED
        target.status = Payment.PaymentStatus.REFUNDED
        target.save(update_fields=["stripe_refund_id", "refund_status", "status"])
        refund: dict[str, t.Any] = {"id": "re_a", "amount": int(target.amount * 100), "metadata": {}}
        # Replay — should be a no-op.
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)
        target.refresh_from_db()
        assert target.status == Payment.PaymentStatus.REFUNDED


class TestRefundsFetchedOutbound:
    """Pinned endpoints (API >= 2022-11-15) deliver charge.refunded without embedded refunds."""

    def test_missing_refunds_list_is_fetched_from_api(self, batch_of_4_online_payments: list[Payment]) -> None:
        """A payload with no refunds key falls back to stripe.Refund.list."""
        payments = batch_of_4_online_payments
        _batch(payments, "pi_pinned")
        target = payments[1]
        refund: dict[str, t.Any] = {"id": "re_api_1", "amount": 4000, "metadata": {"ticket_id": str(target.ticket_id)}}
        event = _charge_event("pi_pinned", refunds=None)

        with patch.object(stripe.Refund, "list") as list_mock:
            list_mock.return_value.auto_paging_iter.return_value = [refund]
            StripeEventHandler(event).handle_charge_refunded(event)

        list_mock.assert_called_once_with(charge="ch_test", limit=100)
        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        assert target.status == Payment.PaymentStatus.REFUNDED

    def test_connected_account_event_forwards_stripe_account(self, batch_of_4_online_payments: list[Payment]) -> None:
        """Connect events fetch the refunds with the Stripe-Account header."""
        payments = batch_of_4_online_payments
        _batch(payments, "pi_pinned_conn")
        target = payments[0]
        refund: dict[str, t.Any] = {"id": "re_api_2", "amount": 4000, "metadata": {"ticket_id": str(target.ticket_id)}}
        event = _charge_event("pi_pinned_conn", refunds=None, account="acct_conn_42")

        with patch.object(stripe.Refund, "list") as list_mock:
            list_mock.return_value.auto_paging_iter.return_value = [refund]
            StripeEventHandler(event).handle_charge_refunded(event)

        list_mock.assert_called_once_with(charge="ch_test", limit=100, stripe_account="acct_conn_42")
        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED

    def test_no_refunds_in_payload_or_api_is_a_noop(self, batch_of_4_online_payments: list[Payment]) -> None:
        """If the API also returns no refunds, the handler warns and mutates nothing."""
        payments = batch_of_4_online_payments
        _batch(payments, "pi_pinned_empty")
        event = _charge_event("pi_pinned_empty", refunds=None)

        with patch.object(stripe.Refund, "list") as list_mock:
            list_mock.return_value.auto_paging_iter.return_value = []
            StripeEventHandler(event).handle_charge_refunded(event)

        for payment in payments:
            payment.refresh_from_db()
            assert payment.refund_status is None
            assert payment.status == Payment.PaymentStatus.SUCCEEDED

    def test_unknown_intent_skips_api_fetch(self) -> None:
        """No Payment rows for the intent → bail before any outbound call."""
        event = _charge_event("pi_nobody_knows", refunds=None)

        with patch.object(stripe.Refund, "list") as list_mock:
            StripeEventHandler(event).handle_charge_refunded(event)

        list_mock.assert_not_called()


class TestNonUniformBatchRefunds:
    """A partial refund on a mixed-price batch must never be guessed by amount.

    Cart = Premium 50.00 (ticket A) + Standard 30.00 (ticket B) on one intent. A
    Stripe-Dashboard refund carries no ``ticket_id`` metadata, so a 30.00 partial
    refund issued *on A* looks exactly like a full refund of B. Auto-cancelling B
    frees a seat its buyer still occupies — a double-sold seat.
    """

    def test_partial_refund_on_mixed_price_batch_does_not_cancel_the_cheap_ticket(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=2)
        ticket_a = ticket_factory(tier=tier)
        ticket_b = ticket_factory(tier=tier)
        payment_a = payment_factory(ticket=ticket_a, amount=Decimal("50.00"))
        payment_b = payment_factory(ticket=ticket_b, amount=Decimal("30.00"))
        _batch([payment_a, payment_b], "pi_mixed")

        # Goodwill refund of 30.00 issued on ticket A from the Stripe Dashboard.
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        event = _charge_event("pi_mixed", [refund])
        with caplog.at_level("WARNING", logger="events.service.stripe_webhook_refunds"):
            StripeEventHandler(event).handle_charge_refunded(event)

        for payment in (payment_a, payment_b):
            payment.refresh_from_db()
            assert payment.refund_status is None
            assert payment.status == Payment.PaymentStatus.SUCCEEDED
            payment.ticket.refresh_from_db()
            assert payment.ticket.status == Ticket.TicketStatus.ACTIVE

        tier.refresh_from_db()
        assert tier.quantity_sold == 2, "no seat may be freed by an ambiguous refund"
        assert any("stripe_refund_non_uniform_batch" in record.message for record in caplog.records)
        assert any("stripe_refund_ambiguous_match" in record.message for record in caplog.records)


class TestRemainingBasedMatching:
    """Branches 3 & 4 compare/sum REMAINING refundable balances, not gross amounts (#870).

    A payment with a prior partial refund has already given back some of its
    ``amount`` — matching (and the Branch-4 fan-out allocation) must reason about
    what's still owed, not what was originally charged.
    """

    def test_branch_4_full_remainder_after_prior_partial_refund_allocates_per_payment_remaining(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        tier = tier_online_with_cancellation_enabled
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=2)
        ticket_a = ticket_factory(tier=tier)
        ticket_b = ticket_factory(tier=tier)
        payment_a = payment_factory(ticket=ticket_a, amount=Decimal("40.00"))
        payment_b = payment_factory(ticket=ticket_b, amount=Decimal("40.00"))
        _batch([payment_a, payment_b], "pi_partial_then_full")

        # Payment A already had a 15.00 partial refund — its remaining is 25.00.
        Refund.objects.create(
            payment=payment_a,
            amount=Decimal("15.00"),
            currency=payment_a.currency,
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )

        # A dashboard refund for exactly the combined remainder: 25.00 (A) + 40.00 (B).
        refund: dict[str, t.Any] = {"id": "re_remainder", "amount": 6500, "metadata": {}}
        event = _charge_event("pi_partial_then_full", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        payment_a.refresh_from_db()
        payment_b.refresh_from_db()
        assert payment_a.status == Payment.PaymentStatus.REFUNDED
        assert payment_b.status == Payment.PaymentStatus.REFUNDED
        row_a = Refund.objects.get(payment=payment_a, stripe_refund_id="re_remainder")
        row_b = Refund.objects.get(payment=payment_b, stripe_refund_id="re_remainder")
        assert row_a.amount == Decimal("25.00"), "allocated its own remaining, not its gross amount"
        assert row_b.amount == Decimal("40.00")

    def test_branch_3_matches_on_remaining_not_gross_amount(
        self,
        payment_factory: t.Callable[..., Payment],
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        """A dashboard refund equal to the sole still-unrefunded payment's REMAINING
        balance matches it, even though that no longer equals its gross amount."""
        tier = tier_online_with_cancellation_enabled
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=2)
        ticket_a = ticket_factory(tier=tier)
        ticket_b = ticket_factory(tier=tier)
        payment_a = payment_factory(ticket=ticket_a, amount=Decimal("40.00"))
        payment_b = payment_factory(ticket=ticket_b, amount=Decimal("40.00"))
        _batch([payment_a, payment_b], "pi_remaining_exact")

        # B is already fully refunded — out of the running (Branch 2.5's guard).
        Payment.objects.filter(pk=payment_b.pk).update(
            status=Payment.PaymentStatus.REFUNDED, refund_status=Payment.RefundStatus.SUCCEEDED
        )
        # A has a prior 30.00 partial refund — its remaining is 10.00, not its 40.00 gross.
        Refund.objects.create(
            payment=payment_a,
            amount=Decimal("30.00"),
            currency=payment_a.currency,
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )

        # 10.00 matches A's remaining exactly — a gross-amount comparison (40.00)
        # would have missed this and fallen through to Branch 5.
        refund: dict[str, t.Any] = {"id": "re_exact_remaining", "amount": 1000, "metadata": {}}
        event = _charge_event("pi_remaining_exact", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        payment_a.refresh_from_db()
        assert payment_a.status == Payment.PaymentStatus.REFUNDED
        row_a = Refund.objects.get(payment=payment_a, stripe_refund_id="re_exact_remaining")
        assert row_a.amount == Decimal("10.00")
