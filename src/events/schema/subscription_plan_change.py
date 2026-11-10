"""Schema for refused staff plan changes / manual tier edits on a subscribed member."""

from ninja import Schema

from events.exceptions import SubscriptionPlanChangeRefusalCode


class SubscriptionPlanChangeRefusedSchema(Schema):
    """409 body for a refused staff plan change or manual tier edit, carrying a stable ``code``.

    ``detail`` is translated and must never be matched on; the frontend branches on ``code``.
    """

    detail: str
    code: SubscriptionPlanChangeRefusalCode
