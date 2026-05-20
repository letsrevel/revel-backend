"""Tests for BatchTicketService capacity counting against pending waitlist offers.

Pending unexpired non-cutoff offers reserve capacity. Cutoff-batch offers do NOT
reserve capacity. The buyer's own offer must not block their own purchase.
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Ticket, TicketTier, WaitlistOffer
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


def _ticket_event(organization: Organization, max_attendees: int = 5) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Capacity Event",
        slug=f"cap-evt-{uuid.uuid4().hex[:8]}",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + dt.timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=max_attendees,
        max_tickets_per_user=10,
        requires_ticket=True,
        waitlist_open=True,
        waitlist_time_window=dt.timedelta(hours=24),
    )


def _free_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def test_pending_offer_blocks_non_holder_purchase(
    organization: Organization,
    revel_user_factory: RevelUserFactory,
) -> None:
    """A non-offer-holder cannot buy the last seat when a non-cutoff offer
    is reserving it."""
    event = _ticket_event(organization, max_attendees=5)
    tier = _free_tier(event)
    # 4 committed tickets - 1 seat left.
    for _ in range(4):
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user_factory(),
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest",
        )
    # 1 pending offer for that last seat.
    holder = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=holder,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    intruder = revel_user_factory()
    service = BatchTicketService(event, tier, intruder)
    items = [TicketPurchaseItem(guest_name="Intruder")]
    with pytest.raises(HttpError) as exc_info:
        service.create_batch(items)
    assert exc_info.value.status_code == 429


def test_offer_holder_can_buy_their_reserved_seat(
    organization: Organization,
    revel_user_factory: RevelUserFactory,
) -> None:
    """The user holding the pending offer is NOT blocked by their own offer."""
    event = _ticket_event(organization, max_attendees=5)
    tier = _free_tier(event)
    for _ in range(4):
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user_factory(),
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest",
        )
    holder: RevelUser = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=holder,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    service = BatchTicketService(event, tier, holder)
    items = [TicketPurchaseItem(guest_name=holder.email)]
    tickets = service.create_batch(items)
    assert isinstance(tickets, list)
    assert len(tickets) == 1


def test_cutoff_offers_do_not_block_purchase(
    organization: Organization,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Cutoff-batch offers race FCFS against real seats; they must NOT reserve.

    A non-offer-holder must still be able to grab the remaining real seat even
    with several cutoff offers outstanding.
    """
    event = _ticket_event(organization, max_attendees=5)
    tier = _free_tier(event)
    for _ in range(4):
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user_factory(),
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest",
        )
    # Several cutoff offers - should not reserve.
    for _ in range(5):
        WaitlistOffer.objects.create(
            event=event,
            user=revel_user_factory(),
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
            is_cutoff_batch=True,
        )

    buyer = revel_user_factory()
    service = BatchTicketService(event, tier, buyer)
    items = [TicketPurchaseItem(guest_name="Buyer")]
    tickets = service.create_batch(items)
    assert isinstance(tickets, list)
    assert len(tickets) == 1
