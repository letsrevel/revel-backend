"""Revel models → neutral provider shapes (spec §7.2). Pure translation, no provider I/O."""

import html
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.utils.html import strip_tags
from django.utils.translation import gettext as _

from common.sanitizers import render_markdown
from events.models import Event, TicketTier
from geo.models import City
from integrations.providers.base import RemoteEvent, RemoteTicketClass, RemoteVenue
from integrations.schema import IntegrationErrorCode, SyncReportEntry

SUMMARY_MAX_CHARS = 140
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_FIRST_PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)


class EventNotEligible(Exception):
    """The event cannot be listed on any external platform (spec §7.1)."""

    def __init__(self, code: IntegrationErrorCode, detail: str) -> None:
        """Initialize with the stable error code and a human-readable detail."""
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MappedTier:
    """A mappable ticket tier paired with its neutral representation."""

    tier: TicketTier
    remote: RemoteTicketClass


@dataclass(frozen=True)
class MappedEvent:
    """The neutral event, its mappable tiers, and the sync report for skipped tiers."""

    remote: RemoteEvent
    tiers: list[MappedTier]
    report: list[SyncReportEntry]


def check_eligible(event: Event) -> None:
    """Raise ``EventNotEligible`` unless the event may be pushed."""
    if event.event_type != Event.EventType.PUBLIC:
        raise EventNotEligible(
            IntegrationErrorCode.EVENT_PRIVATE, _("Only public events can be listed on external platforms.")
        )
    if event.is_open_ended:
        raise EventNotEligible(
            IntegrationErrorCode.EVENT_OPEN_ENDED, _("The event needs an end time to be listed externally.")
        )
    if not event.requires_ticket:
        raise EventNotEligible(
            IntegrationErrorCode.EVENT_NO_TICKETS, _("Only ticketed events can be listed externally.")
        )


def event_timezone(event: Event) -> str:
    """IANA zone for the listing: the event city's zone when known, else the instance default."""
    city = event.city
    if city is not None and city.timezone:
        return str(city.timezone)
    return str(settings.TIME_ZONE)


def summary_from_markdown(md: str | None) -> str:
    """Plain-text first sentence of the description, truncated to the provider cap.

    Headings render as their own block before the first paragraph, so the first
    ``<p>`` is used as the summary source when one is present (falling back to the
    full rendered text otherwise) — this keeps a leading "# Title" out of the summary.
    """
    if not md:
        return ""
    rendered = render_markdown(md)
    match = _FIRST_PARAGRAPH.search(rendered)
    fragment = match.group(1) if match else rendered
    text = html.unescape(strip_tags(fragment)).strip()
    if not text:
        return ""
    first = _SENTENCE_END.split(text, maxsplit=1)[0].strip()
    if len(first) > SUMMARY_MAX_CHARS:
        return first[: SUMMARY_MAX_CHARS - 1] + "…"
    return first


def _venue(event: Event) -> RemoteVenue | None:
    if event.is_virtual or not (event.address or event.city_id):
        return None
    point = event.location
    return RemoteVenue(
        name=event.name,
        address=event.address or "",
        city=event.city.name if event.city else "",
        country=event.city.country if event.city else "",
        latitude=point.y if point else None,
        longitude=point.x if point else None,
    )


def _tier_skip(tier: TicketTier, event: Event, currency: str) -> tuple[IntegrationErrorCode, str] | None:
    if tier.price_type != TicketTier.PriceType.FIXED:
        return IntegrationErrorCode.TIER_VARIABLE_PRICE, _("Pay-what-you-can tiers cannot be listed externally.")
    if tier.restricted_to_membership_tiers.exists():
        return IntegrationErrorCode.TIER_MEMBERS_ONLY, _("Membership-restricted tiers cannot be listed externally.")
    if tier.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE:
        return IntegrationErrorCode.TIER_SEATED, _("Seated tiers cannot be listed externally.")
    if tier.payment_method not in (TicketTier.PaymentMethod.ONLINE, TicketTier.PaymentMethod.FREE):
        return IntegrationErrorCode.TIER_OFFLINE_PAYMENT, _(
            "Tiers paid offline or at the door cannot be listed externally."
        )
    if tier.total_quantity is None and event.max_attendees == 0:
        return IntegrationErrorCode.TIER_NO_CAPACITY, _(
            "Set a quantity on the tier or a maximum attendance on the event."
        )
    if tier.currency != currency:
        return IntegrationErrorCode.TIER_CURRENCY_MISMATCH, _(
            "The platform allows one currency per event; this tier uses another."
        )
    return None


def _majority_currency(tiers: list[TicketTier]) -> str:
    if not tiers:
        return str(settings.DEFAULT_CURRENCY)
    counts = Counter(tier.currency for tier in tiers)
    top = max(counts.values())
    return next(tier.currency for tier in tiers if counts[tier.currency] == top)  # first tier wins ties


def map_event(event: Event, *, remote_paused: dict[UUID, bool], remote_tier_ids: dict[UUID, str]) -> MappedEvent:
    """Build the neutral event and its mappable tiers; unmappable tiers become report entries."""
    tiers = list(
        event.ticket_tiers.prefetch_related("restricted_to_membership_tiers").order_by("display_order", "name")
    )
    currency = _majority_currency(tiers)
    report: list[SyncReportEntry] = []
    mapped: list[MappedTier] = []
    for tier in tiers:
        skip = _tier_skip(tier, event, currency)
        if skip is not None:
            code, detail = skip
            report.append(SyncReportEntry(scope="tier", tier_id=tier.id, tier_name=tier.name, code=code, detail=detail))
            continue
        quantity = tier.total_quantity if tier.total_quantity is not None else event.max_attendees
        mapped.append(
            MappedTier(
                tier=tier,
                remote=RemoteTicketClass(
                    remote_id=remote_tier_ids.get(tier.id),
                    name=tier.name,
                    price=Decimal(tier.price),
                    currency=tier.currency,
                    is_free=tier.price == 0,
                    quantity_total=quantity,
                    sales_start=tier.sales_start_at,
                    sales_end=tier.sales_end_at,
                    hidden=tier.visibility != TicketTier.Visibility.PUBLIC
                    or tier.sales_paused
                    or remote_paused.get(tier.id, False),
                    description=tier.description or "",
                ),
            )
        )
    if not event.cover_art:
        report.append(
            SyncReportEntry(
                scope="event", code=IntegrationErrorCode.IMAGE_MISSING, detail=_("The listing has no cover image yet.")
            )
        )
    remote = RemoteEvent(
        name=event.name,
        summary=summary_from_markdown(event.description),
        description_html=render_markdown(event.description),
        start=event.start,
        end=event.end,
        timezone=event_timezone(event),
        is_virtual=event.is_virtual,
        venue=_venue(event),
        currency=currency,
    )
    return MappedEvent(remote=remote, tiers=mapped, report=report)


def nearest_city(latitude: float, longitude: float, *, max_km: int = 50) -> City | None:
    """Closest known city to a coordinate, for imports (spec §7.6)."""
    point = Point(longitude, latitude, srid=4326)
    return (
        City.objects.filter(location__distance_lte=(point, D(km=max_km)))
        .annotate(distance=Distance("location", point))
        .order_by("distance")
        .first()
    )
