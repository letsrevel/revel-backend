"""Schemas for organization subscription metrics."""

from decimal import Decimal

from ninja import Schema
from pydantic import AwareDatetime


class SubscriptionStatusBreakdownSchema(Schema):
    """Per-status count breakdown for subscription metrics."""

    pending: int
    active: int
    paused: int
    past_due: int
    cancelled: int
    expired: int


class SubscriptionMetricsSchema(Schema):
    """Aggregated subscription metrics for an organization."""

    as_of: AwareDatetime
    active_count: int
    mrr: Decimal
    mrr_currency: str
    mixed_currency_warning: bool
    new_subscribers_30d: int
    churned_30d: int
    churn_rate_30d: float
    status_breakdown: SubscriptionStatusBreakdownSchema
