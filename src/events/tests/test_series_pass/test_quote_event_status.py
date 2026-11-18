"""The pass quote is not purchasable while an upcoming covered event is not OPEN."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, SeriesPass, SeriesPassTierLink, TicketTier
from events.service.series_pass_service import get_quote

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def covered_events(organization: Organization, event_series: EventSeries, series_pass: SeriesPass) -> list[Event]:
    series_pass.visibility = SeriesPass.Visibility.PUBLIC
    series_pass.save(update_fields=["visibility"])
    events = []
    for i in range(2):
        event = Event.objects.create(
            organization=organization,
            name=f"Quote {i}",
            slug=f"quote-status-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=10,
            start=timezone.now() + timedelta(days=7 + i),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(event=event, name="T", price=Decimal("5.00"), currency="EUR")
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
        events.append(event)
    return events


def test_quote_purchasable_when_all_open(series_pass: SeriesPass, covered_events: list[Event]) -> None:
    assert get_quote(series_pass).purchasable is True


def test_quote_not_purchasable_when_upcoming_event_not_open(
    series_pass: SeriesPass, covered_events: list[Event]
) -> None:
    Event.objects.filter(pk=covered_events[1].pk).update(status=Event.EventStatus.DRAFT)

    quote = get_quote(series_pass)

    assert quote.purchasable is False
    assert quote.reason == "Event is not open."
    assert get_quote(series_pass, check_event_status=False).purchasable is True


def test_quote_endpoint_reports_not_purchasable(
    organization_owner_client: Client, series_pass: SeriesPass, covered_events: list[Event]
) -> None:
    Event.objects.filter(pk=covered_events[0].pk).update(status=Event.EventStatus.DRAFT)

    response = organization_owner_client.get(reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.id}))

    assert response.status_code == 200
    assert response.json()["purchasable"] is False
    assert response.json()["reason"] == "Event is not open."
