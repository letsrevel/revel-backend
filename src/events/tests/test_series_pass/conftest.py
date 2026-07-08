from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeries, Organization, RecurrenceRule, SeriesPass, TicketTier


@pytest.fixture
def event_series(organization: Organization) -> EventSeries:
    return EventSeries.objects.create(organization=organization, name="Weekly Classes", slug="weekly-classes")


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def recurring_series(organization: Organization) -> EventSeries:
    """An EventSeries wired to a RecurrenceRule (series passes are not supported on these)."""
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],
        dtstart=timezone.now(),
    )
    return EventSeries.objects.create(
        organization=organization, name="Recurring Classes", slug="recurring-classes", recurrence_rule=rule
    )


@pytest.fixture
def private_event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Private Event",
        slug="private-event",
        event_type=Event.EventType.PRIVATE,
        visibility=Event.Visibility.PRIVATE,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def closed_event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Closed Event",
        slug="closed-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.DRAFT,
        requires_ticket=True,
    )


@pytest.fixture
def no_ticket_event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        name="RSVP Event",
        slug="rsvp-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.OPEN,
        requires_ticket=False,
    )


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


@pytest.fixture
def foreign_series(organization: Organization) -> EventSeries:
    """An EventSeries in the same organization, distinct from event_series."""
    return EventSeries.objects.create(organization=organization, name="Different Series", slug="different-series")


@pytest.fixture
def foreign_event(organization: Organization, foreign_series: EventSeries) -> Event:
    """An event belonging to foreign_series (not event_series)."""
    return Event.objects.create(
        organization=organization,
        name="Foreign Event",
        slug="foreign-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=foreign_series,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
