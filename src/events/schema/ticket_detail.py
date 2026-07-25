"""Ticket, payment, and check-in schemas."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field

from accounts.schema import MemberUserSchema, MinimalRevelUserSchema
from common.signing import get_file_url
from events import models
from events.models import DiscountCode, Payment, Ticket

from .event import MinimalEventSchema
from .organization import MinimalOrganizationMemberSchema
from .ticket_tier import Currencies, TicketTierSchema
from .venue import MinimalSeatSchema


class PaymentSchema(ModelSchema):
    """Public representation of a Payment record."""

    status: Payment.PaymentStatus
    currency: Currencies
    stripe_dashboard_url: str

    class Meta:
        model = Payment
        exclude = ["user", "ticket", "raw_response"]


class MinimalPaymentSchema(ModelSchema):
    """Minimal payment info for inclusion in ticket schemas."""

    status: Payment.PaymentStatus

    class Meta:
        model = Payment
        fields = ["id", "status"]


class TicketDiscountCodeSchema(ModelSchema):
    """Minimal discount-code info for inclusion in admin ticket views."""

    discount_type: DiscountCode.DiscountType

    class Meta:
        model = DiscountCode
        fields = ["id", "code", "discount_type", "discount_value", "currency"]


class TicketSeriesPassSchema(Schema):
    """Minimal series-pass info for tickets materialized from a held series pass."""

    held_pass_id: UUID
    series_pass_id: UUID
    name: str


def _resolve_ticket_series_pass(obj: Ticket) -> TicketSeriesPassSchema | None:
    """Resolve the series pass a ticket was materialized from, if any.

    Shared by ``AdminTicketSchema`` and ``UserTicketSchema`` — both expose the same
    ``series_pass`` shape.
    """
    held_pass = obj.held_pass
    if held_pass is None:
        return None
    return TicketSeriesPassSchema(
        held_pass_id=held_pass.id, series_pass_id=held_pass.series_pass_id, name=held_pass.series_pass.name
    )


class AdminTicketSchema(ModelSchema):
    """Schema for pending tickets in admin interface.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    user: MemberUserSchema
    tier: TicketTierSchema
    payment: PaymentSchema | None = None
    guest_name: str
    seat: MinimalSeatSchema | None = None
    membership: MinimalOrganizationMemberSchema | None = None
    price_paid: Decimal | None = None
    discount_code: TicketDiscountCodeSchema | None = None
    discount_amount: Decimal | None = None
    offline_refund_amount: Decimal | None = None
    series_pass: TicketSeriesPassSchema | None = None

    class Meta:
        model = Ticket
        fields = [
            "id",
            "status",
            "tier",
            "created_at",
            "guest_name",
            "seat",
            "price_paid",
            "discount_amount",
            "offline_refund_amount",
        ]

    @staticmethod
    def resolve_membership(obj: Ticket) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None

    resolve_series_pass: t.ClassVar = staticmethod(_resolve_ticket_series_pass)


class UserTicketSchema(ModelSchema):
    """Schema for user's own tickets with event details.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    event: MinimalEventSchema
    tier: TicketTierSchema
    status: Ticket.TicketStatus
    apple_pass_available: bool
    guest_name: str
    payment: MinimalPaymentSchema | None = None
    seat: MinimalSeatSchema | None = None
    price_paid: Decimal | None = None
    discount_amount: Decimal | None = None
    pdf_url: str | None = None
    pkpass_url: str | None = None
    series_pass: TicketSeriesPassSchema | None = None

    class Meta:
        model = Ticket
        fields = [
            "id",
            "status",
            "tier",
            "created_at",
            "checked_in_at",
            "guest_name",
            "seat",
            "price_paid",
            "discount_amount",
        ]

    @staticmethod
    def resolve_payment(obj: Ticket) -> Payment | None:
        """Resolve payment for pending tickets."""
        if hasattr(obj, "payment"):
            return obj.payment
        return None

    @staticmethod
    def resolve_pdf_url(obj: Ticket) -> str | None:
        """Resolve cached PDF file to signed URL."""
        return get_file_url(obj.pdf_file)

    @staticmethod
    def resolve_pkpass_url(obj: Ticket) -> str | None:
        """Resolve cached pkpass file to signed URL."""
        return get_file_url(obj.pkpass_file)

    resolve_series_pass: t.ClassVar = staticmethod(_resolve_ticket_series_pass)


class CheckInRequestSchema(Schema):
    """Schema for ticket check-in requests."""

    ticket_id: UUID


class CheckInResponseSchema(ModelSchema):
    """Schema for ticket check-in response."""

    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None
    price_paid: Decimal | None = None
    seat: MinimalSeatSchema | None = None
    sector_name: str | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "checked_in_at", "tier", "price_paid", "seat"]

    @staticmethod
    def resolve_sector_name(obj: Ticket) -> str | None:
        """Sector name for door staff redirecting attendees ("Stalls, Row C seat 12")."""
        return obj.sector.name if obj.sector is not None else None


class ConfirmPaymentSchema(Schema):
    """Optional payload for confirming offline/at-the-door ticket payment.

    price_paid is required for PWYC tiers and must be omitted for fixed-price tiers.
    """

    price_paid: Decimal | None = Field(None, gt=0)
