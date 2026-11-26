"""Covered-event helpers shared by the series pass status tests (#1494, #1496, #1536)."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from events.models import Event, SeriesPass, SeriesPassTierLink, TicketTier

OPEN = Event.EventStatus.OPEN
CANCELLED = Event.EventStatus.CANCELLED


def cover(series_pass: SeriesPass, slug: str, days: int, status: Event.EventStatus = OPEN) -> tuple[Event, TicketTier]:
    """Create a covered event and its FREE tier. The link is created directly, past the coverage gate."""
    series = series_pass.event_series
    event = Event.objects.create(
        organization=series.organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=days),
        status=status,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {slug}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return event, tier


def cancel(event: Event) -> None:
    Event.objects.filter(pk=event.pk).update(status=CANCELLED)
