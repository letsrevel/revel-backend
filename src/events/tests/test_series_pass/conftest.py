from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeries, Organization, SeriesPass, TicketTier


@pytest.fixture
def event_series(organization: Organization) -> EventSeries:
    return EventSeries.objects.create(organization=organization, name="Weekly Classes", slug="weekly-classes")


@pytest.fixture
def series_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season Ticket",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def revel_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="pass_holder@example.com", email="pass_holder@example.com")


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Series Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def other_event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Other Event",
        slug="other-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status="open",
        requires_ticket=True,
    )


@pytest.fixture
def other_event_tier(other_event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=other_event,
        name="Other Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
