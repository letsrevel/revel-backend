"""Tests for the series pass enable-time coverage gate (events/service/series_pass_service.py)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

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


def test_nonexistent_event_id_rejected(series_pass: SeriesPass, ticket_tier: TicketTier) -> None:
    """A random, nonexistent event id is rejected with a clear coverage error (not a 500).

    ``add_tier_links`` used to resolve events via a plain ``pk__in`` filter that
    silently drops unknown ids, so a bogus id never reached the coverage gate at all.
    """
    with pytest.raises(SeriesPassCoverageError, match="do not exist"):
        series_pass_service.add_tier_links(series_pass, [{"event_id": uuid4(), "tier_id": ticket_tier.id}])


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


# ---- add_tier_links: idempotency (F3) ----


def test_relink_same_tier_is_idempotent_noop(series_pass: SeriesPass, event: Event, ticket_tier: TicketTier) -> None:
    """Re-sending an already-linked (event, tier) pair is a silent no-op: no error,
    no new row, no materialization dispatch."""
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)

    with patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay:
        created = series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": ticket_tier.id}])

    assert created == []
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event).count() == 1
    mock_delay.assert_not_called()


def test_relink_different_tier_rejected(series_pass: SeriesPass, event: Event, ticket_tier: TicketTier) -> None:
    """Re-covering an already-linked event with a DIFFERENT tier is rejected — silently
    repointing coverage could strand tickets already materialized under the old tier."""
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)
    other_tier = TicketTier.objects.create(
        event=event,
        name="Other Tier For Same Event",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    with pytest.raises(SeriesPassCoverageError, match="different tier"):
        series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": other_tier.id}])
    assert SeriesPassTierLink.objects.get(series_pass=series_pass, event=event).tier_id == ticket_tier.id


def test_mixed_new_and_existing_links_only_new_created_and_dispatched(
    series_pass: SeriesPass,
    event: Event,
    ticket_tier: TicketTier,
    other_event: Event,
    other_event_tier: TicketTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A batch mixing an already-linked pair with a genuinely new one only creates
    (and dispatches materialization for) the new one."""
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)

    with (
        patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        created = series_pass_service.add_tier_links(
            series_pass,
            [
                {"event_id": event.id, "tier_id": ticket_tier.id},
                {"event_id": other_event.id, "tier_id": other_event_tier.id},
            ],
        )

    assert [link.event_id for link in created] == [other_event.id]
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass).count() == 2
    mock_delay.assert_called_once_with(str(series_pass.id), [str(other_event.id)])
