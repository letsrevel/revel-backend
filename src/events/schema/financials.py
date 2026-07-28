"""Schemas for the live financials endpoints (#551 addendum)."""

import datetime as dt
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime


class RateBucketSchema(Schema):
    vat_rate: Decimal
    label: str
    net: Decimal
    vat: Decimal
    gross: Decimal
    ticket_count: int


class CurrencyFinancialsSchema(Schema):
    currency: str
    gross: Decimal
    refunds: Decimal
    net: Decimal
    net_taxable: Decimal
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
    """Membership subscription money for one currency.

    ``net`` is gross minus refunds (the platform fee is reported, not deducted),
    matching the ticket-side convention so the two are addable.
    """

    currency: str
    gross: Decimal
    platform_fee: Decimal
    net: Decimal
    payment_count: int
    refunded_amount: Decimal


class CombinedTotalsSchema(Schema):
    """Grand total for one currency: ticket net plus membership net."""

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
