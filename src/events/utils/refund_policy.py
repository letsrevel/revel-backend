"""Pydantic schemas + helpers for the per-tier ticket refund policy.

This module is pure utility — no DB access, no imports from services.
Safe to import from models, admin, and service code alike.
"""

import typing as t
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class RefundPolicyTier(BaseModel):
    """One bracket of the refund policy: refund X% if cancelling at least N hours before event."""

    hours_before_event: int = Field(ge=0)
    refund_percentage: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class RefundPolicy(BaseModel):
    """A tier's cancellation refund policy.

    ``tiers`` must be ordered by strictly-descending ``hours_before_event`` and
    monotonically non-increasing ``refund_percentage`` (a tier offering a higher
    refund percentage later in time than an earlier bracket is rejected).
    """

    tiers: list[RefundPolicyTier] = Field(min_length=1)
    flat_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _validate_monotonic(self) -> t.Self:
        """Validate that tiers are ordered correctly.

        Raises:
            ValueError: if ``hours_before_event`` is not strictly descending, or if
                ``refund_percentage`` is not monotonically non-increasing across tiers.
        """
        for i in range(1, len(self.tiers)):
            prev = self.tiers[i - 1]
            curr = self.tiers[i]
            if curr.hours_before_event >= prev.hours_before_event:
                raise ValueError("hours_before_event must be strictly descending across tiers")
            if curr.refund_percentage > prev.refund_percentage:
                raise ValueError("refund_percentage must be monotonically non-increasing across tiers")
        return self


def validate_refund_policy(data: dict[str, t.Any] | None) -> RefundPolicy | None:
    """Parse & validate a stored/inbound refund policy dict.

    Args:
        data: Raw dict (e.g. from JSONField) or None.

    Returns:
        ``RefundPolicy`` instance, or ``None`` when ``data`` is ``None``.

    Raises:
        pydantic.ValidationError: if ``data`` is malformed.
    """
    if data is None:
        return None
    return RefundPolicy.model_validate(data)
