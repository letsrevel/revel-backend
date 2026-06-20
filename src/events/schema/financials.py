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


class OrganizationFinancialsSchema(Schema):
    date_from: dt.date
    date_to: dt.date
    active_currency: str | None = None
    available_currencies: list[str]
    totals: list[CurrencyFinancialsSchema]
    events: list[EventFinancialsSchema]
