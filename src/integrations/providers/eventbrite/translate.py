"""Neutral shapes ⇄ Eventbrite v3 JSON. The only module that knows Eventbrite field names (spec §3.1)."""

import typing as t
from datetime import UTC, datetime
from decimal import Decimal

from events.utils.currency import to_stripe_amount
from integrations.providers.base import RemoteEvent, RemoteEventSummary, RemoteStatus, RemoteTicketClass, RemoteVenue

_LIVE_STATUSES = {"live", "started", "ended", "completed"}


def iso_z(dt: datetime) -> str:
    """Eventbrite wants second-precision UTC with a literal ``Z``."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_z(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def minor_units(price: Decimal, currency: str) -> int:
    """Major → smallest currency unit, half-up.

    Delegates to the shared ISO-4217 helper rather than hand-rolling ``price * 100``: zero-decimal
    currencies (JPY, KRW, ...) take no scaling at all, and Eventbrite reads ``cost`` in the same
    smallest-unit convention Stripe does. Hard-coding ×100 listed a ¥1,000 tier at ¥100,000.
    """
    return to_stripe_amount(price, currency)


def status_from_eventbrite(status: str) -> RemoteStatus:
    """Collapse Eventbrite's lifecycle into the three states Revel tracks."""
    if status == "canceled":
        return "cancelled"
    if status in _LIVE_STATUSES:
        return "live"
    return "draft"


def to_eventbrite_event(event: RemoteEvent, *, venue_id: str | None) -> dict[str, t.Any]:
    """Create/update body. Never sends the legacy ``description`` (conflicts with ``summary``).

    ``venue_id`` is omitted rather than blanked when there is no venue. Eventbrite has no
    "physical event with no venue" state and ignores an attempt to clear the field — verified
    against the live API, where both ``venue_id: ""`` and ``venue_id: null`` leave the old venue
    attached. What actually detaches it is ``online_event: true``, which ``is_virtual`` already
    carries, so a Revel event turning virtual drops its remote venue correctly.

    The residual case is a *physical* event whose address and city are both cleared: Eventbrite
    keeps showing the previous venue because the state cannot be expressed. Detaching would mean
    deleting and recreating the listing, which would drop its URL and its orders.
    """
    body: dict[str, t.Any] = {
        "name": {"html": event.name},
        "start": {"timezone": event.timezone, "utc": iso_z(event.start)},
        "end": {"timezone": event.timezone, "utc": iso_z(event.end)},
        "currency": event.currency,
        "listed": event.listed,
        "online_event": event.is_virtual,
    }
    if event.summary:
        body["summary"] = event.summary
    if venue_id:
        body["venue_id"] = venue_id
    return {"event": body}


def to_eventbrite_venue(venue: RemoteVenue) -> dict[str, t.Any]:
    """Build the ``POST /venues/`` create body."""
    body: dict[str, t.Any] = {
        "name": venue.name,
        "address": {
            "address_1": venue.address,
            "city": venue.city,
            "postal_code": venue.postal_code,
            "country": venue.country,
        },
    }
    if venue.latitude is not None and venue.longitude is not None:
        # Coordinates belong to the address sub-object; top-level ``venue.latitude`` is rejected
        # as "Unknown parameter" by the live API (the GET shape echoes them in both places).
        body["address"]["latitude"] = str(venue.latitude)
        body["address"]["longitude"] = str(venue.longitude)
    return {"venue": body}


def to_eventbrite_ticket_class(tc: RemoteTicketClass) -> dict[str, t.Any]:
    """Build the ticket class create/update body: free or ``currency,minor`` cost, never both."""
    body: dict[str, t.Any] = {"name": tc.name, "quantity_total": tc.quantity_total, "hidden": tc.hidden}
    if tc.is_free:
        body["free"] = True
    else:
        body["cost"] = f"{tc.currency},{minor_units(tc.price, tc.currency)}"
    if tc.sales_start:
        body["sales_start"] = iso_z(tc.sales_start)
    if tc.sales_end:
        body["sales_end"] = iso_z(tc.sales_end)
    if tc.description:
        body["description"] = tc.description
    return {"ticket_class": body}


def to_eventbrite_structured_content(html: str) -> dict[str, t.Any]:
    """Wrap raw HTML as a single published text module for the structured-content endpoint."""
    return {"modules": [{"type": "text", "data": {"body": {"text": html, "alignment": "left"}}}], "publish": True}


def from_eventbrite_structured_content(data: dict[str, t.Any]) -> str:
    """Concatenate the text modules of a structured-content page into one HTML blob.

    The modern Eventbrite editor writes the body here and leaves the legacy ``description.html``
    null, so an import that reads only the legacy field lands a Revel draft with no description.
    """
    parts: list[str] = []
    for module in data.get("modules") or []:
        if not isinstance(module, dict) or module.get("type") != "text":
            continue
        text = ((module.get("data") or {}).get("body") or {}).get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


def from_eventbrite_ticket_class(data: dict[str, t.Any]) -> RemoteTicketClass:
    """Map a ticket class resource to the neutral shape."""
    cost = data.get("cost") or {}
    free = bool(data.get("free"))
    return RemoteTicketClass(
        remote_id=str(data["id"]),
        name=str(data.get("name") or ""),
        price=Decimal("0") if free or not cost else Decimal(str(cost.get("major_value", "0"))),
        currency=str(cost.get("currency") or ""),
        is_free=free,
        quantity_total=int(data.get("quantity_total") or 0),
        quantity_sold=int(data.get("quantity_sold") or 0),
        sales_start=_parse_z(data["sales_start"]) if data.get("sales_start") else None,
        sales_end=_parse_z(data["sales_end"]) if data.get("sales_end") else None,
        hidden=bool(data.get("hidden")),
        description=str(data.get("description") or ""),
    )


def _venue_from(data: dict[str, t.Any] | None) -> RemoteVenue | None:
    if not data:
        return None
    addr = data.get("address") or {}
    lat, lon = data.get("latitude"), data.get("longitude")
    return RemoteVenue(
        name=str(data.get("name") or ""),
        address=str(addr.get("address_1") or ""),
        city=str(addr.get("city") or ""),
        postal_code=str(addr.get("postal_code") or ""),
        country=str(addr.get("country") or ""),
        latitude=float(lat) if lat else None,
        longitude=float(lon) if lon else None,
    )


def from_eventbrite_event(data: dict[str, t.Any]) -> RemoteEvent:
    """Map an ``expand=venue,ticket_classes`` event resource to the neutral shape."""
    start, end = data["start"], data["end"]
    description = data.get("description") or {}
    return RemoteEvent(
        remote_id=str(data["id"]),
        account_id=str(data.get("organization_id") or ""),
        name=str((data.get("name") or {}).get("text") or ""),
        summary=str(data.get("summary") or ""),
        description_html=str(description.get("html") or ""),
        start=_parse_z(start["utc"]),
        end=_parse_z(end["utc"]),
        timezone=str(start.get("timezone") or "UTC"),
        is_virtual=bool(data.get("online_event")),
        listed=bool(data.get("listed", True)),
        venue=_venue_from(data.get("venue")),
        currency=str(data.get("currency") or ""),
        status=status_from_eventbrite(str(data.get("status") or "draft")),
        url=str(data.get("url") or ""),
        ticket_classes=[from_eventbrite_ticket_class(tc) for tc in data.get("ticket_classes") or []],
    )


def from_eventbrite_summary(data: dict[str, t.Any]) -> RemoteEventSummary:
    """Map a list-endpoint event resource to the neutral summary shape."""
    return RemoteEventSummary(
        remote_id=str(data["id"]),
        name=str((data.get("name") or {}).get("text") or ""),
        start=_parse_z(data["start"]["utc"]),
        status=status_from_eventbrite(str(data.get("status") or "draft")),
        url=str(data.get("url") or ""),
    )
