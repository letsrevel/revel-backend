"""Unit tests for cancellation_service.quote_cancellation and build_cancellation_preview."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from events.models import Payment, Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
from events.service.cancellation_service import (
    CancellationBlocked,
    CancellationNotOwner,
    StripeRefundFailed,
    build_cancellation_preview,
    cancel_ticket_by_user,
    quote_cancellation,
)

pytestmark = pytest.mark.django_db


def _policy(*tiers: tuple[int, str], flat_fee: str = "0") -> dict[str, t.Any]:
    return {
        "tiers": [{"hours_before_event": hours, "refund_percentage": pct} for hours, pct in tiers],
        "flat_fee": flat_fee,
    }


class TestQuoteCancellationBlockReasons:
    def test_already_cancelled(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CANCELLED)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.ALREADY_CANCELLED
        assert result.refund_amount == Decimal("0")

    def test_checked_in(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CHECKED_IN)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.CHECKED_IN

    def test_event_started(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
        event: t.Any,
    ) -> None:
        event.start = timezone.now() - timedelta(minutes=1)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.EVENT_STARTED

    def test_tier_not_permitting_cancellation(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_disabled: TicketTier,
        event: t.Any,
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        ticket = ticket_factory(tier=tier_online_with_cancellation_disabled)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.NOT_PERMITTED

    def test_past_deadline(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
    ) -> None:
        # Event starts in 10 hours, deadline is 24 hours before → we're past it.
        event.start = timezone.now() + timedelta(hours=10)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        tier = tier_factory(
            allow_user_cancellation=True,
            cancellation_deadline_hours=24,
            refund_policy=_policy((0, "100")),
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=tier.refund_policy)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.PAST_DEADLINE


class TestQuoteCancellationRefundMath:
    def test_no_snapshot_means_zero_refund_but_can_cancel(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
        event: t.Any,
    ) -> None:
        # Snapshot = None → refund 0, cancellation still permitted.
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, refund_policy_snapshot=None)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_first_tier_branch(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        # 200 hours before event, tier at 168h=100% applies.
        event.start = timezone.now() + timedelta(hours=200)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("40.00")

    def test_middle_tier_branch(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("20.00")

    def test_no_tier_matches_yields_zero_refund_still_cancellable(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=10)  # past all tiers
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((48, "50"), (24, "25"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_flat_fee_floored_at_zero(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((48, "50"), flat_fee="100")  # base_refund < flat_fee
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("0")

    def test_offline_tier_always_zero_refund(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((48, "100"))
        tier = tier_factory(
            allow_user_cancellation=True,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            refund_policy=policy,
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is True
        assert result.refund_amount == Decimal("0")

    def test_snapshot_authority_ignores_mutated_tier_policy(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        frozen_policy = _policy((48, "100"))
        tier = tier_factory(allow_user_cancellation=True, refund_policy=frozen_policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=frozen_policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        # Mutate the tier policy post-purchase to something worse.
        tier.refund_policy = _policy((48, "0"))
        tier.save(update_fields=["refund_policy"])
        result = quote_cancellation(ticket, timezone.now())
        assert result.refund_amount == Decimal("40.00")


class TestBuildCancellationPreview:
    def test_three_tier_policy_produces_three_windows(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., t.Any],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=300)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = _policy((168, "100"), (48, "50"), (24, "25"), flat_fee="1")
        tier = tier_factory(allow_user_cancellation=True, refund_policy=policy)
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment_factory(ticket=ticket, amount=Decimal("40.00"))
        preview = build_cancellation_preview(ticket, timezone.now())
        assert preview.can_cancel is True
        assert [w.refund_percentage for w in preview.windows] == [Decimal("100"), Decimal("50"), Decimal("25")]
        # Flat fee subtracted on each window
        assert preview.windows[0].refund_amount == Decimal("39.00")
        assert preview.windows[1].refund_amount == Decimal("19.00")
        assert preview.windows[2].refund_amount == Decimal("9.00")

    def test_empty_policy_yields_empty_windows(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        ticket = ticket_factory(
            tier=tier_online_with_cancellation_enabled,
            refund_policy_snapshot=None,
        )
        preview = build_cancellation_preview(ticket, timezone.now())
        assert preview.windows == []


class TestCancelTicketByUser:
    def test_wrong_user_raises_not_owner(
        self,
        ticket_factory: t.Callable[..., Ticket],
        nonmember_user: t.Any,
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        with pytest.raises(CancellationNotOwner):
            cancel_ticket_by_user(ticket, nonmember_user, reason="", now=timezone.now())

    def test_block_reason_raises_cancellation_blocked(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_enabled: TicketTier,
    ) -> None:
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled, status=Ticket.TicketStatus.CANCELLED)
        with pytest.raises(CancellationBlocked) as info:
            cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())
        assert info.value.reason == CancellationBlockReason.ALREADY_CANCELLED

    def test_free_ticket_cancels_no_stripe_call(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.FREE,
            price=Decimal("0"),
            allow_user_cancellation=True,
        )
        ticket = ticket_factory(tier=tier)
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        with patch("stripe.Refund.create") as mock_create:
            result = cancel_ticket_by_user(ticket, ticket.user, reason="moved", now=timezone.now())
        assert mock_create.call_count == 0
        assert result.refund_amount == Decimal("0")
        assert result.refund_status is None
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        assert ticket.cancelled_by_id == ticket.user_id
        assert ticket.cancellation_source == "user"
        assert ticket.cancellation_reason == "moved"
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_online_ticket_calls_stripe_with_idempotency_key(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = {"tiers": [{"hours_before_event": 48, "refund_percentage": "100"}], "flat_fee": "0"}
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price=Decimal("40.00"),
            allow_user_cancellation=True,
            refund_policy=policy,
        )
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment = payment_factory(ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_123")
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_abc"
            result = cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["payment_intent"] == "pi_123"
        assert kwargs["amount"] == 4000
        assert kwargs["idempotency_key"] == f"refund:{ticket.id}"
        assert kwargs["metadata"] == {"ticket_id": str(ticket.id), "user_initiated": "true"}
        assert result.refund_status == Payment.RefundStatus.PENDING
        payment.refresh_from_db()
        assert payment.stripe_refund_id == "re_abc"
        assert payment.refund_amount == Decimal("40.00")
        assert payment.refund_status == Payment.RefundStatus.PENDING

    def test_stripe_failure_rolls_back_transaction(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        event: t.Any,
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        import stripe

        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=1)
        event.save(update_fields=["start", "end"])
        policy = {"tiers": [{"hours_before_event": 48, "refund_percentage": "100"}], "flat_fee": "0"}
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price=Decimal("40.00"),
            allow_user_cancellation=True,
            refund_policy=policy,
        )
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        ticket = ticket_factory(tier=tier, refund_policy_snapshot=policy)
        payment = payment_factory(ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_err")

        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            with pytest.raises(StripeRefundFailed):
                cancel_ticket_by_user(ticket, ticket.user, reason="", now=timezone.now())

        ticket.refresh_from_db()
        payment.refresh_from_db()
        tier.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert payment.refund_status is None
        assert tier.quantity_sold == 1
