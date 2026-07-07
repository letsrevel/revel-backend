"""Tests for the series pass enable-time coverage gate (events/service/series_pass_service.py)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from events.exceptions import SeriesPassCoverageError
from events.models import Event, EventSeries, OrganizationQuestionnaire, SeriesPass, SeriesPassTierLink, TicketTier
from events.schema.series_pass import SeriesPassCreateSchema, SeriesPassTierLinkInputSchema
from events.service import series_pass_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def series_pass_payload(event: Event, ticket_tier: TicketTier) -> SeriesPassCreateSchema:
    """A creation payload covering ``event`` via ``ticket_tier``."""
    return SeriesPassCreateSchema(
        name="Season Ticket",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        tier_links=[SeriesPassTierLinkInputSchema(event_id=event.id, tier_id=ticket_tier.id)],
    )


def test_recurring_series_rejected(recurring_series: EventSeries, series_pass_payload: SeriesPassCreateSchema) -> None:
    """A recurring series can never carry a series pass, regardless of the covered events."""
    with pytest.raises(SeriesPassCoverageError, match="recurring"):
        series_pass_service.create_series_pass(recurring_series, series_pass_payload)


def test_event_not_open_rejected(event_series: EventSeries, closed_event: Event) -> None:
    """A non-OPEN event fails the coverage gate."""
    with pytest.raises(SeriesPassCoverageError, match="not open"):
        series_pass_service.validate_events_coverable(event_series, [closed_event])


def test_event_from_different_series_rejected(event_series: EventSeries, foreign_event: Event) -> None:
    """An event belonging to a different series fails the coverage gate."""
    with pytest.raises(SeriesPassCoverageError, match="does not belong to this series"):
        series_pass_service.validate_events_coverable(event_series, [foreign_event])


def test_event_requires_ticket_false_rejected(event_series: EventSeries, no_ticket_event: Event) -> None:
    """An RSVP-only event (requires_ticket=False) fails the coverage gate."""
    with pytest.raises(SeriesPassCoverageError, match="require a ticket"):
        series_pass_service.validate_events_coverable(event_series, [no_ticket_event])


def test_private_event_rejected(event_series: EventSeries, private_event: Event) -> None:
    """A PRIVATE (invitation-only) event fails the coverage gate."""
    with pytest.raises(SeriesPassCoverageError, match="invitation"):
        series_pass_service.validate_events_coverable(event_series, [private_event])


def test_admission_questionnaire_on_event_rejected(
    event_series: EventSeries, event: Event, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An admission questionnaire targeting the event blocks coverage."""
    org_questionnaire.events.add(event)
    with pytest.raises(SeriesPassCoverageError, match="admission questionnaire"):
        series_pass_service.validate_events_coverable(event_series, [event])


def test_admission_questionnaire_on_series_rejected(
    event_series: EventSeries, event: Event, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An admission questionnaire targeting the series blocks coverage of any of its events."""
    org_questionnaire.event_series.add(event_series)
    with pytest.raises(SeriesPassCoverageError, match="admission questionnaire"):
        series_pass_service.validate_events_coverable(event_series, [event])


def test_happy_path_creates_links(series_pass: SeriesPass, event: Event, ticket_tier: TicketTier) -> None:
    """A well-formed link (matching event/series/currency/seat-mode) is created."""
    created = series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": ticket_tier.id}])
    assert len(created) == 1
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event, tier=ticket_tier).exists()


def test_tier_from_another_event_rejected(series_pass: SeriesPass, event: Event, other_event_tier: TicketTier) -> None:
    """Linking a tier that belongs to a different event bubbles the model clean() ValidationError."""
    with pytest.raises(ValidationError):
        series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": other_event_tier.id}])


def test_currency_mismatch_rejected(series_pass: SeriesPass, event: Event) -> None:
    """Linking a tier whose currency differs from the pass's currency bubbles the model ValidationError."""
    mismatched_tier = TicketTier.objects.create(
        event=event,
        name="USD Tier",
        price=Decimal("10.00"),
        currency="USD",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    with pytest.raises(ValidationError):
        series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": mismatched_tier.id}])


def test_create_series_pass_happy_path(event_series: EventSeries, series_pass_payload: SeriesPassCreateSchema) -> None:
    """create_series_pass builds the pass and its tier links in one call."""
    created = series_pass_service.create_series_pass(event_series, series_pass_payload)
    assert created.pk is not None
    assert created.tier_links.count() == 1
