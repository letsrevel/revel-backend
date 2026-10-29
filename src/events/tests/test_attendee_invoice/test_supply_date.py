"""Date / period of supply snapshot on attendee invoices (BT-72 / BG-14, #1234)."""

import datetime
import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.gis.geos import Point
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.utils.supply_date import supply_dates_for
from geo.models import City

pytestmark = pytest.mark.django_db

UTC = datetime.UTC


def _set_times(event: Event, start: datetime.datetime, end: datetime.datetime) -> Event:
    Event.objects.filter(pk=event.pk).update(start=start, end=end)
    event.refresh_from_db()
    return event


@pytest.fixture
def vienna_city() -> City:
    return City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=424242,
        location=Point(16.3738, 48.2082, srid=4326),
        timezone="Europe/Vienna",
    )


class TestSupplyDatesFor:
    def test_single_day_event_sets_supply_date_only(self, event: Event) -> None:
        _set_times(
            event,
            datetime.datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
            datetime.datetime(2026, 11, 20, 22, 0, tzinfo=UTC),
        )
        assert supply_dates_for(event) == (datetime.date(2026, 11, 20), None)

    def test_multi_day_event_sets_period(self, event: Event) -> None:
        _set_times(
            event,
            datetime.datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
            datetime.datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        )
        assert supply_dates_for(event) == (datetime.date(2026, 11, 20), datetime.date(2026, 11, 22))

    def test_supply_date_uses_event_timezone(self, event: Event, vienna_city: City) -> None:
        event.city = vienna_city
        event.save()
        _set_times(
            event, datetime.datetime(2026, 12, 31, 23, 30, tzinfo=UTC), datetime.datetime(2027, 1, 1, 3, 0, tzinfo=UTC)
        )
        assert supply_dates_for(event) == (datetime.date(2027, 1, 1), None)

    def test_event_without_city_uses_utc(self, event: Event) -> None:
        _set_times(
            event, datetime.datetime(2026, 12, 31, 23, 30, tzinfo=UTC), datetime.datetime(2027, 1, 1, 3, 0, tzinfo=UTC)
        )
        assert supply_dates_for(event) == (datetime.date(2026, 12, 31), datetime.date(2027, 1, 1))

    def test_open_ended_event_never_claims_a_period(self, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(
            start=datetime.datetime(2026, 11, 20, 20, 0, tzinfo=UTC),
            end=datetime.datetime(2026, 11, 21, 20, 0, tzinfo=UTC),
            is_open_ended=True,
        )
        event.refresh_from_db()
        assert supply_dates_for(event) == (datetime.date(2026, 11, 20), None)

    def test_event_crossing_midnight_keeps_period(self, event: Event) -> None:
        _set_times(
            event,
            datetime.datetime(2026, 11, 20, 20, 0, tzinfo=UTC),
            datetime.datetime(2026, 11, 21, 2, 0, tzinfo=UTC),
        )
        assert supply_dates_for(event) == (datetime.date(2026, 11, 20), datetime.date(2026, 11, 21))


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_invoice_snapshots_supply_date(
    _mail: t.Any,
    _pdf: t.Any,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    _set_times(
        event, datetime.datetime(2026, 11, 20, 10, 0, tzinfo=UTC), datetime.datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    )
    _create_payment(
        user=member_user,
        event=event,
        tier=event_ticket_tier,
        session_id="cs_supply",
        buyer_billing_snapshot=_default_billing_snapshot(),
    )
    invoice = generate_attendee_invoice("cs_supply")
    assert invoice is not None
    assert invoice.supply_date == datetime.date(2026, 11, 20)
    assert invoice.supply_period_end == datetime.date(2026, 11, 22)


def test_pdf_prints_period(organization: Organization, event: Event, member_user: RevelUser) -> None:
    inv = _create_draft(organization, event, member_user)
    inv.supply_date = datetime.date(2026, 11, 20)
    inv.supply_period_end = datetime.date(2026, 11, 22)
    html = render_to_string("invoices/attendee_invoice.html", {"invoice": inv})
    assert "Period of supply" in html
    assert "2026-11-20 – 2026-11-22" in html


def test_pdf_prints_single_date(organization: Organization, event: Event, member_user: RevelUser) -> None:
    inv = _create_draft(organization, event, member_user)
    inv.supply_date = datetime.date(2026, 11, 20)
    html = render_to_string("invoices/attendee_invoice.html", {"invoice": inv})
    assert "Date of supply" in html
    assert "Period of supply" not in html


def test_legacy_invoice_without_supply_date_renders(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    inv = _create_draft(organization, event, member_user)
    assert inv.supply_date is None
    html = render_to_string("invoices/attendee_invoice.html", {"invoice": inv})
    assert "of supply" not in html


def test_credit_note_prints_invoice_period(organization: Organization, event: Event, member_user: RevelUser) -> None:
    inv = _create_draft(organization, event, member_user)
    inv.supply_date = datetime.date(2026, 11, 20)
    inv.supply_period_end = datetime.date(2026, 11, 22)
    credit_note = AttendeeInvoiceCreditNote(
        invoice=inv,
        credit_note_number="CN-1",
        amount_gross=Decimal("1.00"),
        amount_net=Decimal("1.00"),
        amount_vat=Decimal("0.00"),
    )
    html = render_to_string("invoices/attendee_credit_note.html", {"credit_note": credit_note, "invoice": inv})
    assert "Period of supply" in html
    assert "2026-11-20 – 2026-11-22" in html
