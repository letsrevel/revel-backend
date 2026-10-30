"""A series pass sold by a small-business-scheme organizer carries no VAT (#1244, review M2)."""

from decimal import Decimal
from uuid import uuid4

import pytest

from accounts.models import RevelUser
from events.models import EventSeries, HeldSeriesPass, Organization, Payment, SeriesPass, TicketTier
from events.service import stripe_service
from events.tests.test_attendee_invoice.test_small_business_scheme import _make_small_business
from events.tests.test_series_pass.test_stripe import _make_tiered_tickets

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_series_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season Ticket",
        price=Decimal("33.33"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


def test_series_pass_payments_have_no_vat_despite_tier_rates(
    stripe_connected_organization: Organization,
    organization: Organization,
    online_series_pass: SeriesPass,
    event_series: EventSeries,
    revel_user: RevelUser,
) -> None:
    """The tiers carry 10% and 22% (copied rates the guard never saw); the pass still books 0%."""
    _make_small_business(stripe_connected_organization)
    held_pass = HeldSeriesPass.objects.create(
        series_pass=online_series_pass,
        user=revel_user,
        price_paid=Decimal("33.33"),
        status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
    )
    tickets = _make_tiered_tickets(organization, event_series, revel_user, held_pass)

    stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=uuid4())

    payments = Payment.objects.filter(ticket__in=tickets)
    assert payments.count() == 5
    assert all(p.vat_amount == 0 and p.vat_rate == 0 and p.net_amount == p.amount for p in payments)
