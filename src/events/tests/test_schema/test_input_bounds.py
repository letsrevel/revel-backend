"""Input-size bounds on request schemas."""

import typing as t
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from pydantic import ValidationError

from events.schema import VenueSectorCreateSchema
from events.schema.organization import OrganizationEditSchema
from events.schema.recurrence_rule import RecurrenceRuleCreateSchema
from events.schema.vat_preview import VATPreviewItemSchema, VATPreviewRequestSchema


def test_sector_polygon_vertex_count_is_capped() -> None:
    shape = [{"x": i, "y": i % 7} for i in range(501)]
    with pytest.raises(ValidationError):
        VenueSectorCreateSchema(name="Floor", shape=shape)  # type: ignore[call-arg,arg-type]


def test_sector_seat_count_is_capped() -> None:
    seats = [{"label": str(i)} for i in range(10_001)]
    with pytest.raises(ValidationError):
        VenueSectorCreateSchema(name="Floor", seats=seats)  # type: ignore[call-arg,arg-type]


def test_vat_preview_item_count_is_capped() -> None:
    with pytest.raises(ValidationError):
        VATPreviewItemSchema(tier_id=uuid4(), count=10**8)  # type: ignore[call-arg]
    assert VATPreviewItemSchema(tier_id=uuid4(), count=5).count == 5  # type: ignore[call-arg]


def test_vat_preview_items_length_is_capped() -> None:
    items = [{"tier_id": str(uuid4()), "count": 1} for _ in range(51)]
    with pytest.raises(ValidationError):
        VATPreviewRequestSchema.model_validate({"billing_info": {"billing_name": "A"}, "items": items})


@pytest.mark.parametrize("field", ["membership_grace_period_days", "membership_subscription_revival_window_days"])
def test_membership_policy_days_are_capped(field: str) -> None:
    payload: dict[str, t.Any] = {"visibility": "public", field: 10**9}
    with pytest.raises(ValidationError):
        OrganizationEditSchema.model_validate(payload)


def test_recurrence_dtstart_lookback_is_bounded() -> None:
    with pytest.raises(ValidationError):
        RecurrenceRuleCreateSchema(frequency="daily", dtstart=timezone.now() - timedelta(days=800))  # type: ignore[arg-type,call-arg]
    RecurrenceRuleCreateSchema(frequency="daily", dtstart=timezone.now() - timedelta(days=30))  # type: ignore[arg-type,call-arg]
