"""Fixtures for the account erasure tests (#1476): a holder with personal data and events at the 120-day edge."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeries, Organization, Payment, Ticket, TicketTier

HOLDER_EMAIL = "holder@example.com"


@pytest.fixture
def holder(revel_user_factory: RevelUserFactory) -> RevelUser:
    """An account with every personal field set."""
    return revel_user_factory.create_user(
        username=HOLDER_EMAIL,
        email=HOLDER_EMAIL,
        first_name="Ada",
        last_name="Lovelace",
        preferred_name="Ada L",
        pronouns="she/her",
        phone_number="+436601234567",
        bio="Hello",
        email_verified=True,
    )


@pytest.fixture
def ended_event_factory(organization: Organization, event_series: EventSeries) -> t.Callable[[timedelta], Event]:
    """An event of the series that ended ``ago`` before now (a negative ``ago`` is in the future)."""

    def _make(ago: timedelta) -> Event:
        end = timezone.now() - ago
        return Event.objects.create(
            organization=organization,
            event_series=event_series,
            name=f"Event {ago}",
            start=end - timedelta(hours=2),
            end=end,
            status="open",
            requires_ticket=True,
        )

    return _make


def make_ticket(user: RevelUser, event: Event, status: str = Ticket.TicketStatus.ACTIVE, **kwargs: t.Any) -> Ticket:
    """A ticket of ``user`` on a fresh online tier of ``event``."""
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {TicketTier.objects.count()}",
        price=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    return Ticket.objects.create(event=event, tier=tier, user=user, status=status, **kwargs)


def make_payment(ticket: Ticket, status: str = Payment.PaymentStatus.SUCCEEDED) -> Payment:
    """A Stripe payment of ``ticket`` by its holder."""
    return Payment.objects.create(
        ticket=ticket,
        user=ticket.user,
        amount=Decimal("10.00"),
        platform_fee=Decimal("0.50"),
        currency="EUR",
        status=status,
        stripe_session_id=f"cs_{ticket.pk}",
        stripe_payment_intent_id=f"pi_{ticket.pk}",
    )
