"""Unit tests for tier admin endpoints accepting cancellation/refund-policy fields."""

import typing as t
from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from events.service import ticket_service

pytestmark = pytest.mark.django_db


def test_create_ticket_tier_persists_cancellation_fields(event: Event) -> None:
    """create_ticket_tier should persist allow_user_cancellation, cancellation_deadline_hours, and refund_policy."""
    tier = ticket_service.create_ticket_tier(
        event=event,
        tier_data={
            "name": "VIP",
            "price": Decimal("50"),
            "allow_user_cancellation": True,
            "cancellation_deadline_hours": 24,
            "refund_policy": {
                "tiers": [{"hours_before_event": 24, "refund_percentage": "50"}],
                "flat_fee": "0",
            },
        },
        restricted_to_membership_tiers_ids=None,
    )
    tier.refresh_from_db()
    assert tier.allow_user_cancellation is True
    assert tier.cancellation_deadline_hours == 24
    assert tier.refund_policy is not None
    assert tier.refund_policy["tiers"][0]["refund_percentage"] == "50"


def test_update_ticket_tier_clears_refund_policy(tier_factory: t.Callable[..., TicketTier]) -> None:
    """update_ticket_tier should clear refund_policy when explicitly set to None."""
    tier = tier_factory(
        refund_policy={
            "tiers": [{"hours_before_event": 0, "refund_percentage": "50"}],
            "flat_fee": "0",
        }
    )
    ticket_service.update_ticket_tier(
        tier=tier, tier_data={"refund_policy": None}, restricted_to_membership_tiers_ids=None
    )
    tier.refresh_from_db()
    assert tier.refund_policy is None
