"""Tests for EventUserEligibility shape extensions."""

import datetime as dt
import uuid

from events.service.event_manager.enums import ReasonCode, Reasons
from events.service.event_manager.types import EventUserEligibility


def test_optional_waitlist_fields_default_none() -> None:
    e = EventUserEligibility(allowed=True, event_id=uuid.uuid4())
    assert e.pending_offers_count is None
    assert e.next_batch_at is None
    assert e.waitlist_position is None
    assert e.active_offer_expires_at is None
    assert e.reason_code is None


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


def test_reasons_and_reason_codes_have_matching_members() -> None:
    """Every Reasons member must have a ReasonCode counterpart with the same name."""
    assert set(Reasons.__members__) == set(ReasonCode.__members__)


def test_reasons_code_property_returns_matching_reasoncode() -> None:
    for reason in Reasons:
        assert reason.code is ReasonCode[reason.name]


def test_reason_code_serializes_to_stable_string() -> None:
    e = EventUserEligibility(
        allowed=False,
        event_id=uuid.uuid4(),
        reason="Event is full.",
        reason_code=Reasons.EVENT_IS_FULL.code,
    )
    dumped = e.model_dump(mode="json")
    assert dumped["reason_code"] == "event_is_full"
