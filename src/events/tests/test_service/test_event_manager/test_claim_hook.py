"""Implicit claim of waitlist offer on successful registration."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from conftest import RevelUserFactory  # type: ignore[import-not-found]
from events.models import Event, EventRSVP, EventWaitList, TicketTier, WaitlistOffer
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.event_manager.manager import EventManager

pytestmark = pytest.mark.django_db


def test_rsvp_yes_claims_active_offer(event: Event, revel_user_factory: RevelUserFactory) -> None:
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = False
    event.max_attendees = 5
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()
    me = revel_user_factory()
    EventWaitList.objects.create(event=event, user=me)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=me,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    EventManager(me, event).rsvp(EventRSVP.RsvpStatus.YES)

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.CLAIMED
    assert offer.claimed_at is not None
    assert not EventWaitList.objects.filter(event=event, user=me).exists()


def test_rsvp_no_does_not_claim_offer(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Setting RSVP to NO must not consume the user's offer."""
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = False
    event.max_attendees = 5
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()
    me = revel_user_factory()
    offer = WaitlistOffer.objects.create(
        event=event,
        user=me,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.PENDING


def test_ticket_creation_claims_active_offer(
    event: Event,
    event_ticket_tier: TicketTier,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Creating a ticket (PENDING or active) via BatchTicketService claims the offer."""
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = True
    event.max_attendees = 5
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()
    me = revel_user_factory()
    EventWaitList.objects.create(event=event, user=me)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=me,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    event_ticket_tier.payment_method = TicketTier.PaymentMethod.FREE
    event_ticket_tier.price = Decimal("0")
    event_ticket_tier.save()

    service = BatchTicketService(event=event, tier=event_ticket_tier, user=me)
    service.create_batch([TicketPurchaseItem(guest_name=me.get_display_name())])

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.CLAIMED
    assert offer.claimed_at is not None
    assert not EventWaitList.objects.filter(event=event, user=me).exists()


def test_ticket_creation_without_offer_is_no_op(
    event: Event,
    event_ticket_tier: TicketTier,
    revel_user_factory: RevelUserFactory,
) -> None:
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = True
    event.max_attendees = 5
    event.save()
    me = revel_user_factory()
    event_ticket_tier.payment_method = TicketTier.PaymentMethod.FREE
    event_ticket_tier.price = Decimal("0")
    event_ticket_tier.save()

    service = BatchTicketService(event=event, tier=event_ticket_tier, user=me)
    # Must not raise even though no offer exists.
    service.create_batch([TicketPurchaseItem(guest_name=me.get_display_name())])
