"""Schema for the revenue & VAT report endpoints (#551)."""

import datetime as dt
from uuid import UUID

from ninja import Schema


class RevenueReportRequestSchema(Schema):
    event_id: UUID | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
