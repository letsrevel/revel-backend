"""Tests for EventUserEligibility shape extensions."""

import datetime as dt
import uuid

from events.service.event_manager.types import EventUserEligibility


def test_optional_waitlist_fields_default_none() -> None:
    e = EventUserEligibility(allowed=True, event_id=uuid.uuid4())
    assert e.pending_offers_count is None
    assert e.next_batch_at is None
    assert e.waitlist_position is None
    assert e.active_offer_expires_at is None


def test_fields_accept_values() -> None:
    now = dt.datetime.now(dt.UTC)
    e = EventUserEligibility(
        allowed=True,
        event_id=uuid.uuid4(),
        pending_offers_count=3,
        next_batch_at=now,
        waitlist_position=2,
        active_offer_expires_at=now,
    )
    assert e.pending_offers_count == 3
    assert e.waitlist_position == 2
    assert e.next_batch_at == now
    assert e.active_offer_expires_at == now
