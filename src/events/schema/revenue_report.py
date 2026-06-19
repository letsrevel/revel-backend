"""Schema for the revenue & VAT report endpoints (#551)."""

import datetime as dt

from ninja import Schema


class RevenueReportRequestSchema(Schema):
    event_id: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
