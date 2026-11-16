"""Daily platform-activity recap for the admin (Pushover + Discord).

Lives in ``events`` because it aggregates across ``accounts`` and ``events`` models, and
``common`` must never import from apps.
"""

import datetime
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce

from accounts.models import RevelUser
from events.models import Event, EventRSVP, MembershipPayment, Organization, Payment, Ticket
from events.service.platform_revenue_service import COLLECTED_STATUSES, FeeTotals, local_midnight, platform_fee_totals

MAX_ORG_NAMES = 10
MAX_ORG_NAME_LEN = 40
MAX_MESSAGE_LEN = 1024  # Pushover's limit; Discord's 2000 is implied


@dataclass(frozen=True)
class PaymentTotals:
    """Succeeded payments: total count plus the summed amount per currency."""

    count: int = 0
    by_currency: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyRecap:
    """Counts of what was created in ``[start, end)``."""

    start: datetime.datetime
    end: datetime.datetime
    registered_users: int
    guest_users: int
    organization_names: list[str]
    events: int
    tickets: int
    rsvps: int
    payments: PaymentTotals
    membership_payments: PaymentTotals
    fees: dict[str, FeeTotals] = field(default_factory=dict)


def previous_local_day_window(now: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    """Return ``[yesterday 00:00, today 00:00)`` in the current time zone (Europe/Vienna) as aware datetimes."""
    return local_midnight(now, days_back=1), local_midnight(now)


def _totals(rows: t.Iterable[t.Mapping[str, t.Any]]) -> PaymentTotals:
    count = 0
    by_currency: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        count += row["n"]
        by_currency[row["currency"]] += row["total"]
    return PaymentTotals(count=count, by_currency=dict(sorted(by_currency.items())))


def build_daily_recap(now: datetime.datetime | None = None) -> DailyRecap:
    """Count what was created during the previous local calendar day.

    "Collected" payments are SUCCEEDED or REFUNDED, the same definition as the fee totals. Ticket payments are
    bucketed by ``created_at`` (``Payment`` has no paid timestamp; checkout creates it and it succeeds minutes
    later, while ``updated_at`` moves on refund). Membership payments use ``occurred_at``, falling back to
    ``created_at``.
    """
    start, end = previous_local_day_window(now)
    window = {"created_at__gte": start, "created_at__lt": end}
    user_counts = RevelUser.objects.filter(date_joined__gte=start, date_joined__lt=end).aggregate(
        guests=Count("pk", filter=Q(guest=True)),
        registered=Count("pk", filter=Q(guest=False)),
    )
    ticket_rows = (
        Payment.objects.filter(status__in=COLLECTED_STATUSES, **window)
        .values("currency")
        .annotate(n=Count("pk"), total=Sum("amount"))
    )
    membership_rows = (
        MembershipPayment.objects.filter(status__in=COLLECTED_STATUSES)
        .annotate(paid_at=Coalesce("occurred_at", "created_at"))
        .filter(paid_at__gte=start, paid_at__lt=end)
        .values("currency")
        .annotate(n=Count("pk"), total=Sum("amount"))
    )
    return DailyRecap(
        start=start,
        end=end,
        registered_users=user_counts["registered"],
        guest_users=user_counts["guests"],
        organization_names=list(
            Organization.objects.filter(**window).order_by("created_at").values_list("name", flat=True)
        ),
        events=Event.objects.filter(**window).count(),
        tickets=Ticket.objects.filter(**window).count(),
        rsvps=EventRSVP.objects.filter(**window).count(),
        payments=_totals(ticket_rows),
        membership_payments=_totals(membership_rows),
        fees=platform_fee_totals(start, end),
    )


def _money(totals: PaymentTotals) -> str:
    return ", ".join(f"{currency} {amount:.2f}" for currency, amount in totals.by_currency.items()) or "none"


def _fees(fees: dict[str, FeeTotals]) -> str:
    return (
        "; ".join(
            f"{f.currency} {f.net:.2f} net + {f.vat:.2f} VAT (set aside) = {f.gross:.2f} gross" for f in fees.values()
        )
        or "none"
    )


def _org_line(names: list[str], fixed_len: int) -> str:
    """Org count plus as many (truncated) names as fit in ``MAX_MESSAGE_LEN`` next to ``fixed_len`` other chars."""
    clipped = [n if len(n) <= MAX_ORG_NAME_LEN else n[:MAX_ORG_NAME_LEN] + "…" for n in names]
    for shown in range(min(len(clipped), MAX_ORG_NAMES), -1, -1):
        line = f"Orgs: {len(names)}"
        if shown:
            more = f" and {len(names) - shown} more" if shown < len(names) else ""
            line += f" ({', '.join(clipped[:shown])}{more})"
        if fixed_len + len(line) + 1 <= MAX_MESSAGE_LEN:
            return line
    return f"Orgs: {len(names)}"


def format_daily_recap(recap: DailyRecap) -> str:
    """Render the recap as a compact plain-text message of at most ``MAX_MESSAGE_LEN`` characters."""
    head = [
        f"Revel daily recap — {recap.start:%a %d %b %Y}",
        f"Users: {recap.registered_users} registered, {recap.guest_users} guests",
    ]
    tail = [
        f"Events: {recap.events} · Tickets: {recap.tickets} · RSVPs: {recap.rsvps}",
        f"Payments collected: {recap.payments.count} ({_money(recap.payments)})",
        f"Membership payments collected: {recap.membership_payments.count} ({_money(recap.membership_payments)})",
        f"Fees: {_fees(recap.fees)}",
    ]
    fixed_len = sum(len(line) + 1 for line in [*head, *tail])
    message = "\n".join([*head, _org_line(recap.organization_names, fixed_len), *tail])
    return message if len(message) <= MAX_MESSAGE_LEN else message[: MAX_MESSAGE_LEN - 1] + "…"
