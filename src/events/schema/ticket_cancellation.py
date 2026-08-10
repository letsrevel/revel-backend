"""Ticket cancellation and refund preview schemas."""

from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field

from common.schema import StrippedString
from events import models
from events.models import Payment, TicketTier
from events.models import Refund as RefundModel

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


# ---- Organizer Online Refund Schemas (#865) ----


class TicketRefundSchema(Schema):
    """A single refund attempt on a ticket payment.

    Named distinctly from ``subscription.RefundSchema`` (a request payload for the
    membership-payment refund endpoint) to avoid a bare-name OpenAPI/import clash —
    see the naming note on ``MembershipPaymentSchema``.
    """

    id: UUID
    amount: Decimal
    currency: str
    status: RefundModel.RefundStatus
    source: RefundModel.Source
    reason: str
    stripe_refund_id: str
    failure_reason: str
    created_at: AwareDatetime


class AdminIssueRefundSchema(Schema):
    """Payload for the organizer online-refund endpoint."""

    amount: Decimal | None = Field(
        default=None,
        gt=0,
        description="Refund amount. Defaults to the full remaining refundable amount.",
    )
    reason: StrippedString | None = Field(default=None, max_length=500)


class TicketRefundContextSchema(Schema):
    """Admin preview: how much was paid, refunded, and remains refundable on a ticket."""

    payment_method: TicketTier.PaymentMethod
    amount_paid: Decimal
    currency: str
    total_refunded: Decimal
    total_pending: Decimal
    remaining_refundable: Decimal
    policy_suggested_amount: Decimal | None = None
    refunds: list[TicketRefundSchema] = Field(default_factory=list)


class RefundPreviewCurrencyLine(Schema):
    """Refund totals vs Stripe balance for one currency."""

    currency: str
    total_refundable: Decimal
    available_balance: Decimal | None = None
    balance_sufficient: bool | None = None


class EventRefundPreviewSchema(Schema):
    """Advisory pre-flight for the cancel-with-refunds flow. Never blocks."""

    active_tickets: int
    online_refundable_tickets: int
    offline_tickets: int
    currencies: list[RefundPreviewCurrencyLine]
    tickets_refund_started_at: AwareDatetime | None = None
