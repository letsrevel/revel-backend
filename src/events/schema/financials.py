"""Schemas for the live financials endpoints (#551 addendum)."""

import datetime as dt
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field


class RateBucketSchema(Schema):
    vat_rate: Decimal
    label: str
    net: Decimal
    vat: Decimal
    gross: Decimal
    ticket_count: int


class CurrencyFinancialsSchema(Schema):
    """Ticket money for one currency. Ticket scope only — membership revenue is not included here.

    Every figure below is derived from ticket payments; membership subscription money is
    reported separately in ``MembershipFinancialsSchema`` and is never folded into these totals.
    """

    currency: str
    gross: Decimal
    refunds: Decimal
    net: Decimal
    net_taxable: Decimal = Field(
        ...,
        description=(
            "VAT taxable base for TICKET revenue only, in this currency. Membership revenue is "
            "excluded: subscription plans carry no VAT rate, so no VAT treatment is applied to "
            "membership money and it cannot be added to this figure. Do not use this value as a "
            "total taxable turnover without consulting your tax advisor."
        ),
    )
    vat: Decimal
    sold_count: int
    refunded_count: int
    rate_buckets: list[RateBucketSchema]


class EventFinancialsSchema(Schema):
    event_id: UUID
    event_name: str
    event_start: AwareDatetime
    by_currency: list[CurrencyFinancialsSchema]


class MembershipFinancialsSchema(Schema):
    """Membership subscription money for one currency, reported GROSS with no VAT treatment.

    ``net`` is gross minus refunds (the platform fee is reported, not deducted),
    matching the ticket-side convention so the two are addable.

    VAT caveat: subscription plans carry no VAT rate, so none of these amounts are
    VAT-decomposed — ``gross``/``net`` are gross-of-VAT figures. Consequently membership
    revenue is excluded from ``CurrencyFinancialsSchema.net_taxable``, which is ticket
    scope only. Do not present these numbers as a taxable base.
    """

    currency: str
    gross: Decimal
    platform_fee: Decimal
    net: Decimal
    payment_count: int
    refunded_amount: Decimal


class CombinedTotalsSchema(Schema):
    """Grand total for one currency: ticket net plus membership net.

    This is a cash-flow total, not a tax figure: ``memberships_net`` is gross of VAT (see
    ``MembershipFinancialsSchema``), so ``net`` mixes a VAT-decomposed ticket component with
    an untreated membership one and must not be filed as taxable turnover.
    """

    currency: str
    tickets_net: Decimal
    memberships_net: Decimal
    net: Decimal


class OrganizationFinancialsSchema(Schema):
    """Org-wide financials: per-event ticket money, org-level membership money, and their sum."""

    date_from: dt.date
    date_to: dt.date
    active_currency: str | None = None
    available_currencies: list[str]
    totals: list[CurrencyFinancialsSchema]
    events: list[EventFinancialsSchema]
    memberships: list[MembershipFinancialsSchema]
    combined_totals: list[CombinedTotalsSchema]
