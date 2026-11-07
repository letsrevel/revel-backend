"""Fixtures for the day-membership (#1320) tests."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, MembershipRecord, MembershipTier, Organization, Ticket, TicketTier


@pytest.fixture
def day_event(organization: Organization, event_series: EventSeries) -> Event:
    """An open event that starts in a week and lasts six hours."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Day Event",
        slug="day-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=start,
        end=start + timedelta(hours=6),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def day_member_tier(organization: Organization) -> MembershipTier:
    """The TEMPORARY tier a day ticket grants."""
    return MembershipTier.objects.create(
        organization=organization, name="Day Member", kind=MembershipTier.Kind.TEMPORARY
    )


@pytest.fixture
def day_tier_factory(day_event: Event, day_member_tier: MembershipTier) -> t.Callable[..., TicketTier]:
    """Tier factory on ``day_event``; grants the day tier unless ``grants=False``."""

    def _make(*, grants: bool = True, **kwargs: t.Any) -> TicketTier:
        defaults: dict[str, t.Any] = {
            "event": day_event,
            "name": f"Tier {TicketTier.objects.filter(event=day_event).count()}",
            "price": Decimal("0.00"),
            "currency": "EUR",
            "payment_method": TicketTier.PaymentMethod.FREE,
        }
        defaults.update(kwargs)
        if grants:
            defaults["grants_membership_tier"] = day_member_tier
            # Granting tiers are limited to one ticket per person (TicketTier validation, #1320).
            defaults.setdefault("max_tickets_per_user", 1)
        return TicketTier.objects.create(**defaults)

    return _make


@pytest.fixture
def day_tier(day_tier_factory: t.Callable[..., TicketTier]) -> TicketTier:
    """A FREE granting tier."""
    return day_tier_factory()


@pytest.fixture
def holder(public_user: RevelUser) -> RevelUser:
    """A person with a legal name, to buy tickets."""
    public_user.first_name, public_user.last_name = "Dana", "Day"
    public_user.save()
    return public_user


@pytest.fixture
def make_ticket(day_event: Event, holder: RevelUser) -> t.Callable[..., Ticket]:
    """Create a ticket on ``day_event`` for ``holder`` through ``save()`` (fires post_save)."""

    def _make(tier: TicketTier, status: str = Ticket.TicketStatus.ACTIVE, **kwargs: t.Any) -> Ticket:
        defaults: dict[str, t.Any] = {
            "event": day_event,
            "user": holder,
            "tier": tier,
            "status": status,
            "guest_name": "Dana Day",
        }
        defaults.update(kwargs)
        return Ticket.objects.create(**defaults)

    return _make


def records_for(ticket: Ticket) -> list[MembershipRecord]:
    """All register rows tied to ``ticket``."""
    return list(MembershipRecord.objects.filter(ticket=ticket))
