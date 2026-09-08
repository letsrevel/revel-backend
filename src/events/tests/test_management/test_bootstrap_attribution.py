"""The e2e fixture tickets carry a deterministic campaign mix (#922)."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.bootstrap_test_events import Command
from events.management.commands.seeder.tickets import ATTRIBUTION_CAMPAIGNS
from events.models import Event, Organization, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def command(db: t.Any) -> Command:
    """A bootstrap command with just the state ``_create_relationships`` reads."""
    owner = RevelUser.objects.create_user(username="owner@test.com", email="owner@test.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=owner)
    now = timezone.now()

    def _event(key: str, **kwargs: t.Any) -> Event:
        return Event.objects.create(
            organization=org, name=key, slug=key, start=now + timedelta(days=7), end=now + timedelta(days=8), **kwargs
        )

    command = Command()
    command.now = now
    command.member_user = RevelUser.objects.create_user(
        username="member@test.com", email="member@test.com", password="x"
    )
    command.events = {
        "full": _event("full"),
        "sold_out": _event("sold-out", requires_ticket=True),
        "private": _event("private", event_type=Event.EventType.PRIVATE),
    }
    # A ticketed event auto-creates its "General Admission" tier, which the command reads by name.
    assert TicketTier.objects.filter(event=command.events["sold_out"], name="General Admission").exists()
    return command


def test_sold_out_event_tickets_carry_a_deterministic_campaign_mix(command: Command) -> None:
    command._create_relationships()

    attributions = list(
        Ticket.objects.filter(event=command.events["sold_out"])
        .order_by("user__username")
        .values_list("attribution", flat=True)
    )
    assert attributions == [
        ATTRIBUTION_CAMPAIGNS["newsletter"],
        ATTRIBUTION_CAMPAIGNS["newsletter"],
        ATTRIBUTION_CAMPAIGNS["instagram"],
        ATTRIBUTION_CAMPAIGNS["partner_embed"],
        None,
    ]
