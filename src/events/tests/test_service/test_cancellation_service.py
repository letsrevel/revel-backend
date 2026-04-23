"""Unit tests for cancellation_service.quote_cancellation and build_cancellation_preview."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from events.models import Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
from events.service.cancellation_service import build_cancellation_preview, quote_cancellation

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
        event.save(update_fields=["start"])
        ticket = ticket_factory(tier=tier_online_with_cancellation_enabled)
        result = quote_cancellation(ticket, timezone.now())
        assert result.can_cancel is False
        assert result.reason == CancellationBlockReason.EVENT_STARTED

    def test_tier_not_permitting_cancellation(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_online_with_cancellation_disabled: TicketTier,
    ) -> None:
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
        event.save(update_fields=["start"])
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
    ) -> None:
        # Snapshot = None → refund 0, cancellation still permitted.
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
        event.save(update_fields=["start"])
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
