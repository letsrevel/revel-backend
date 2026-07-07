"""Per-ticket payment/cancellation flows must reject series-pass tickets (400).

Pass tickets carry no per-ticket payment: confirming, cancelling or refunding one
through the per-ticket admin endpoints would desync the pass and its counters —
those operations go through the pass-level endpoints instead.
"""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventSeries, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.service import ticket_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def offline_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Guarded Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Guarded Offline Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def pass_ticket(event: Event, offline_tier: TicketTier, offline_pass: SeriesPass, revel_user: RevelUser) -> Ticket:
    held = HeldSeriesPass.objects.create(
        series_pass=offline_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.PENDING,
        price_paid=offline_pass.price,
    )
    return Ticket.objects.create(
        event=event,
        tier=offline_tier,
        user=revel_user,
        held_pass=held,
        status=Ticket.TicketStatus.PENDING,
        guest_name="Pass Holder",
    )


def test_confirm_ticket_payment_rejects_pass_ticket(pass_ticket: Ticket) -> None:
    with pytest.raises(HttpError) as exc_info:
        ticket_service.confirm_ticket_payment(pass_ticket)
    assert exc_info.value.status_code == 400
    assert "series pass" in str(exc_info.value)

    pass_ticket.refresh_from_db()
    assert pass_ticket.status == Ticket.TicketStatus.PENDING


def test_cancel_offline_ticket_rejects_pass_ticket(pass_ticket: Ticket, organization_owner_user: RevelUser) -> None:
    with pytest.raises(HttpError) as exc_info:
        ticket_service.cancel_offline_ticket(pass_ticket, cancelled_by=organization_owner_user)
    assert exc_info.value.status_code == 400

    pass_ticket.refresh_from_db()
    assert pass_ticket.status == Ticket.TicketStatus.PENDING


def test_mark_offline_ticket_refunded_rejects_pass_ticket(
    pass_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    with pytest.raises(HttpError) as exc_info:
        ticket_service.mark_offline_ticket_refunded(pass_ticket, cancelled_by=organization_owner_user)
    assert exc_info.value.status_code == 400

    pass_ticket.refresh_from_db()
    assert pass_ticket.status == Ticket.TicketStatus.PENDING
