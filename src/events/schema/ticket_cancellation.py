"""Ticket cancellation and refund preview schemas."""

from decimal import Decimal

from ninja import Schema
from pydantic import AwareDatetime, Field

from common.schema import StrippedString
from events import models
from events.models import Payment, TicketTier

from .ticket_detail import UserTicketSchema
from .ticket_tier import RefundPolicySchema

# ---- Cancellation Schemas ----


class RefundWindowSchema(Schema):
    """A single active refund window: the percentage and absolute amount refundable until a deadline."""

    refund_percentage: Decimal
    refund_amount: Decimal
    effective_until: AwareDatetime


class CancellationPreviewSchema(Schema):
    """Preview of what a user would receive if they cancelled their ticket now."""

    can_cancel: bool
    reason: models.ticket.CancellationBlockReason | None = None
    refund_amount: Decimal
    currency: str
    deadline: AwareDatetime | None = None
    flat_fee: Decimal
    payment_method: TicketTier.PaymentMethod
    windows: list[RefundWindowSchema] = Field(default_factory=list)
    policy_snapshot: RefundPolicySchema | None = None


class TicketCancellationRequestSchema(Schema):
    """Optional payload sent when a user cancels their own ticket."""

    reason: StrippedString | None = Field(default=None, max_length=500)


class TicketCancellationResponseSchema(Schema):
    """Response returned after a successful user-initiated ticket cancellation."""

    ticket: UserTicketSchema
    refund_amount: Decimal
    currency: str
    refund_status: Payment.RefundStatus | None = None


class CancellationBlockedErrorSchema(Schema):
    """Error body returned when cancellation is not permitted."""

    code: models.ticket.CancellationBlockReason
    detail: str


class AdminCancelTicketSchema(Schema):
    """Optional payload for the admin cancel endpoint."""

    cancellation_reason: StrippedString | None = Field(default=None, max_length=500)


class AdminRefundTicketSchema(AdminCancelTicketSchema):
    """Optional payload for the admin mark-refunded endpoint."""

    refund_amount: Decimal | None = Field(
        default=None,
        ge=0,
        description="Explicit amount refunded. Defaults to the amount paid when omitted.",
    )
