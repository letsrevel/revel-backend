"""The ticket seeder stamps a realistic campaign mix so attribution is visible at a glance (#922)."""

import io
import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.state import SeederState
from events.management.commands.seeder.tickets import TicketSeeder
from events.models import Event, Organization, Ticket, TicketTier
from events.schema.attribution import sanitize_attribution


@pytest.fixture
def seeded_state(db: t.Any) -> SeederState:
    """One ticketed event with a free tier and a pool of buyers."""
    users = [
        RevelUser.objects.create_user(username=f"buyer{i}", email=f"buyer{i}@example.com", password="x")
        for i in range(40)
    ]
    org = Organization.objects.create(name="Org", slug="org", owner=users[0])
    now = timezone.now()
    event = Event.objects.create(
        organization=org, name="Event", slug="event", start=now + timedelta(days=3), end=now + timedelta(days=4)
    )
    tier = TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        total_quantity=200,
    )
    state = SeederState()
    state.organizations = [org]
    state.users = users
    state.ticketed_events = [event]
    state.ticket_tiers = {event.id: [tier]}
    return state


def _run(state: SeederState, weights: dict[str, float] | None = None) -> None:
    config = SeederConfig(seed=1234)
    config.sold_out_event_pct = 0.0
    if weights is not None:
        config.ticket_attribution_weights = weights
    TicketSeeder(config=config, state=state, stdout=io.StringIO())._create_tickets()


@pytest.mark.django_db
def test_default_mix_has_both_attributed_and_direct_tickets(seeded_state: SeederState) -> None:
    _run(seeded_state)

    attributions = list(Ticket.objects.values_list("attribution", flat=True))
    assert len(attributions) >= 10
    assert any(a is None for a in attributions), "some tickets must be direct"
    assert any(a is not None for a in attributions), "some tickets must carry campaign tags"
    assert len({str(a) for a in attributions if a}) >= 2, "more than one campaign in the mix"


@pytest.mark.django_db
def test_seeded_tags_survive_the_api_sanitiser(seeded_state: SeederState) -> None:
    """Seed data must look exactly like what a real checkout would have stored."""
    _run(seeded_state)

    for attribution in Ticket.objects.exclude(attribution=None).values_list("attribution", flat=True):
        assert isinstance(attribution, dict)
        assert sanitize_attribution(attribution) == attribution


@pytest.mark.django_db
def test_direct_only_weights_leave_every_ticket_untagged(seeded_state: SeederState) -> None:
    _run(seeded_state, {"direct": 1.0})

    assert Ticket.objects.count() >= 10
    assert not Ticket.objects.exclude(attribution=None).exists()
