"""``BatchTicketService`` stamps the checkout's attribution on every ticket (#922)."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketAttribution, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService, CartGroup

pytestmark = pytest.mark.django_db

ATTRIBUTION: TicketAttribution = {"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": "spring-2026"}


def test_single_tier_form_stamps_every_ticket(
    batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser
) -> None:
    service = BatchTicketService(batch_event, batch_offline_tier, batch_user, attribution=ATTRIBUTION)
    result = service.create_batch([TicketPurchaseItem(guest_name="A"), TicketPurchaseItem(guest_name="B")])
    assert isinstance(result, list)

    stored = list(Ticket.objects.filter(pk__in=[ticket.pk for ticket in result]))
    assert len(stored) == 2
    assert all(ticket.attribution == ATTRIBUTION for ticket in stored)


def test_cart_form_stamps_every_group(
    batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser
) -> None:
    second_tier = TicketTier.objects.create(
        event=batch_event,
        name="Offline Second",
        price=Decimal("15.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=100,
    )
    groups = [
        CartGroup(tier=batch_offline_tier, items=[TicketPurchaseItem(guest_name="A")]),
        CartGroup(tier=second_tier, items=[TicketPurchaseItem(guest_name="B")]),
    ]
    service = BatchTicketService(batch_event, user=batch_user, groups=groups, attribution=ATTRIBUTION)
    result = service.create_batch()
    assert isinstance(result, list)

    assert {ticket.tier_id for ticket in result} == {batch_offline_tier.pk, second_tier.pk}
    assert all(Ticket.objects.get(pk=ticket.pk).attribution == ATTRIBUTION for ticket in result)


def test_no_attribution_stays_null(batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser) -> None:
    service = BatchTicketService(batch_event, batch_offline_tier, batch_user)
    result = service.create_batch([TicketPurchaseItem(guest_name="A")])
    assert isinstance(result, list)
    assert Ticket.objects.get(pk=result[0].pk).attribution is None
