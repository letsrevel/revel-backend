"""Tests for BatchTicketService's reserve-only online checkout path (#632).

Covers: online create_batch reserves (PENDING tickets + PENDING payments, no
Stripe call) and returns a reservation_id; attendee VAT (VIES) is resolved
strictly before the TicketTier row is locked.
"""

import typing as t
from decimal import Decimal
from unittest import mock
from uuid import UUID

import pytest

from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.schema.ticket import BuyerBillingInfoSchema
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid_ticket_tier(event: Event, organization: Organization) -> TicketTier:
    """An ONLINE ticket tier on a Stripe-connected organization."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return TicketTier.objects.create(
        event=event,
        name="Online Purchase",
        price=Decimal("50.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


def test_online_create_batch_returns_reservation_and_makes_pending(
    event: Event, paid_ticket_tier: TicketTier, member_user: t.Any
) -> None:
    """Online create_batch reserves (PENDING tickets + PENDING payments) and returns a reservation_id, no Stripe."""
    svc = BatchTicketService(event, paid_ticket_tier, member_user)
    with mock.patch("stripe.checkout.Session.create") as create:
        result = svc.create_batch([TicketPurchaseItem(guest_name="A")])
        create.assert_not_called()
    assert isinstance(result, tuple)
    tickets, reservation_id = result
    assert isinstance(reservation_id, UUID)
    assert all(tk.status == Ticket.TicketStatus.PENDING for tk in tickets)
    assert Payment.objects.filter(reservation_id=reservation_id).count() == len(tickets)
    paid_ticket_tier.refresh_from_db()
    assert paid_ticket_tier.quantity_sold == len(tickets)


def test_online_create_batch_resolves_vat_before_locking_tier(
    event: Event, paid_ticket_tier: TicketTier, member_user: t.Any
) -> None:
    """Attendee VAT (VIES) must be resolved BEFORE the TicketTier select_for_update lock (#632).

    Otherwise the contended tier row would be held across the VIES network round-trip.
    """
    call_order: list[str] = []

    def record_vat_resolve(*args: t.Any, **kwargs: t.Any) -> tuple[None, bool]:
        call_order.append("resolve_attendee_vat_for_reserve")
        return None, False

    original_select_for_update = TicketTier.objects.select_for_update

    def record_select_for_update(*args: t.Any, **kwargs: t.Any) -> t.Any:
        call_order.append("select_for_update")
        return original_select_for_update(*args, **kwargs)

    billing_info = BuyerBillingInfoSchema(billing_name="Acme Corp", vat_id="12345678901", vat_country_code="DE")

    svc = BatchTicketService(event, paid_ticket_tier, member_user)
    with (
        mock.patch("events.service.stripe_service.resolve_attendee_vat_for_reserve", side_effect=record_vat_resolve),
        mock.patch.object(TicketTier.objects, "select_for_update", side_effect=record_select_for_update),
        mock.patch("events.service.stripe_service.reserve_batch_payments"),
    ):
        svc.create_batch([TicketPurchaseItem(guest_name="A")], billing_info=billing_info)

    assert call_order.index("resolve_attendee_vat_for_reserve") < call_order.index("select_for_update")
