"""Tickets created via BatchTicketService snapshot the tier's refund_policy."""

import typing as t

import pytest

from events.models import Ticket, TicketTier

pytestmark = pytest.mark.django_db


def test_offline_batch_purchase_snapshots_refund_policy(
    batch_offline_tier: TicketTier,
    run_checkout_offline: t.Callable[..., list[Ticket]],
) -> None:
    """Tickets bulk-created for a tier with a refund policy copy it into the snapshot field."""
    batch_offline_tier.allow_user_cancellation = True
    batch_offline_tier.refund_policy = {
        "tiers": [{"hours_before_event": 24, "refund_percentage": "100"}],
        "flat_fee": "0",
    }
    batch_offline_tier.save(update_fields=["allow_user_cancellation", "refund_policy"])
    tickets = run_checkout_offline(count=2)
    assert len(tickets) == 2
    for ticket in tickets:
        assert ticket.refund_policy_snapshot == batch_offline_tier.refund_policy


def test_null_policy_snapshots_as_none(
    batch_offline_tier: TicketTier,
    run_checkout_offline: t.Callable[..., list[Ticket]],
) -> None:
    """Tickets created for a tier with no refund policy have a null snapshot."""
    assert batch_offline_tier.refund_policy is None
    tickets = run_checkout_offline(count=1)
    assert tickets[0].refund_policy_snapshot is None
