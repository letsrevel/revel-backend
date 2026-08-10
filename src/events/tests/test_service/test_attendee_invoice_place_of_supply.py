"""Tests for the invoice-side place-of-supply rules (#868/#869).

Covers:
- ``generate_attendee_invoice`` snapshotting ``seller_vat_country``: the event's
  country for physical admission, the org's establishment for virtual events.
- ``_is_virtual_interim`` — the gate for the EU B2C interim-treatment wording on
  virtual-event invoices, including its wiring into the PDF template context.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.gis.geos import Point

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier, Venue
from events.models.attendee_invoice import AttendeeInvoice
from events.models.ticket import Payment
from events.service.attendee_invoice_service import _is_virtual_interim, generate_attendee_invoice
from geo.models import City

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"


def _make_org_invoicing_ready(org: Organization) -> Organization:
    """Configure an org with all prerequisites for invoicing (IT seller, 22%)."""
    org.vat_country_code = "IT"
    org.vat_id = "IT12345678901"
    org.vat_id_validated = True
    org.vat_rate = Decimal("22.00")
    org.billing_name = "ACME SRL"
    org.billing_address = "Via Roma 1, 00100 Roma"
    org.billing_email = "billing@acme.it"
    org.contact_email = "info@acme.it"
    org.invoicing_mode = Organization.InvoicingMode.HYBRID
    org.save()
    return org


def _make_city(iso2: str, city_id: int, name: str) -> City:
    """Create a City row with the minimum fields the model requires."""
    return City.objects.create(
        name=name,
        ascii_name=name,
        country=name,
        iso2=iso2,
        iso3=f"{iso2}X",
        city_id=city_id,
        location=Point(13.40, 52.52),
        population=1000,
    )


def _create_payment(
    *,
    user: RevelUser,
    event: Event,
    tier: TicketTier,
    session_id: str,
    buyer_billing_snapshot: dict[str, t.Any] | None = None,
) -> Payment:
    """Create a ticket and a succeeded payment for invoice generation."""
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, guest_name="Test Guest")
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session_id,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=Decimal("100.00"),
        net_amount=Decimal("81.97"),
        vat_amount=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        platform_fee=Decimal("5.00"),
        currency="EUR",
        buyer_billing_snapshot=buyer_billing_snapshot,
    )


def _billing_snapshot(country: str = "DE", reverse_charge: bool = False) -> dict[str, t.Any]:
    return {
        "billing_name": "Buyer GmbH",
        "vat_id": f"{country}123456789",
        "vat_country_code": country,
        "vat_id_validated": False,
        "billing_address": "Berliner Str. 1, 10115 Berlin",
        "billing_email": "buyer@example.de",
        "reverse_charge": reverse_charge,
    }


# ---------------------------------------------------------------------------
# seller_vat_country snapshot
# ---------------------------------------------------------------------------


class TestSellerVatCountrySnapshot:
    """Physical events invoice from the event's country; virtual from the org's."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_physical_event_with_foreign_venue_uses_event_country(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A physical IT-org event held in DE snapshots seller_vat_country=DE."""
        _make_org_invoicing_ready(organization)
        venue = Venue.objects.create(organization=organization, name="Hall", city=_make_city("DE", 920001, "Berlin"))
        event.venue = venue
        event.save(update_fields=["venue"])
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_pos_phys_venue",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_pos_phys_venue")

        assert invoice is not None
        assert invoice.seller_vat_country == "DE"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_physical_event_with_foreign_city_uses_event_country(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Without a venue, the event's own city drives the seller VAT country."""
        _make_org_invoicing_ready(organization)
        event.city = _make_city("AT", 920002, "Vienna")
        event.save(update_fields=["city"])
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_pos_phys_city",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_pos_phys_city")

        assert invoice is not None
        assert invoice.seller_vat_country == "AT"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_virtual_event_uses_org_country_despite_foreign_venue(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A virtual event is supplied from the org's establishment, wherever it streams from."""
        _make_org_invoicing_ready(organization)
        venue = Venue.objects.create(organization=organization, name="Studio", city=_make_city("DE", 920003, "Koeln"))
        event.venue = venue
        event.is_virtual = True
        event.save(update_fields=["venue", "is_virtual"])
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_pos_virtual",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_pos_virtual")

        assert invoice is not None
        assert invoice.seller_vat_country == "IT"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_physical_event_without_location_falls_back_to_org_country(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """No venue, no city: effective_vat_country falls back to the org's country."""
        _make_org_invoicing_ready(organization)
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_pos_fallback",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_pos_fallback")

        assert invoice is not None
        assert invoice.seller_vat_country == "IT"


# ---------------------------------------------------------------------------
# _is_virtual_interim truth table
# ---------------------------------------------------------------------------


class TestIsVirtualInterim:
    """The interim-treatment wording gate for virtual-event invoices."""

    @staticmethod
    def _invoice(event: Event | None, **overrides: t.Any) -> AttendeeInvoice:
        """An unsaved invoice shaped like a virtual EU B2C interim document."""
        fields: dict[str, t.Any] = {
            "event": event,
            "buyer_vat_country": "DE",
            "seller_vat_country": "IT",
            "reverse_charge": False,
            "total_vat": Decimal("18.03"),
        }
        fields.update(overrides)
        return AttendeeInvoice(**fields)

    @pytest.fixture
    def virtual_event(self, event: Event) -> Event:
        """The generic event, made virtual."""
        event.is_virtual = True
        event.save(update_fields=["is_virtual"])
        return event

    def test_virtual_eu_cross_border_b2c_is_interim(self, virtual_event: Event) -> None:
        """Virtual event, EU buyer abroad, VAT charged, no RC -> True."""
        assert _is_virtual_interim(self._invoice(virtual_event)) is True

    def test_reverse_charged_invoice_is_not_interim(self, virtual_event: Event) -> None:
        """A reverse-charged (B2B) invoice gets RC wording, not the interim one."""
        assert (
            _is_virtual_interim(self._invoice(virtual_event, reverse_charge=True, total_vat=Decimal("0.00"))) is False
        )

    def test_domestic_buyer_is_not_interim(self, virtual_event: Event) -> None:
        """A buyer in the seller's own country owes exactly what was charged."""
        assert _is_virtual_interim(self._invoice(virtual_event, buyer_vat_country="IT")) is False

    def test_non_eu_buyer_is_not_interim(self, virtual_event: Event) -> None:
        """A non-EU buyer is outside EU VAT scope — no OSS disclosure."""
        assert _is_virtual_interim(self._invoice(virtual_event, buyer_vat_country="US")) is False

    def test_physical_event_is_not_interim(self, event: Event) -> None:
        """Physical admission is taxed at the event — the interim wording never applies."""
        assert event.is_virtual is False
        assert _is_virtual_interim(self._invoice(event)) is False

    def test_invoice_without_event_is_not_interim(self) -> None:
        """No event on the invoice: nothing to derive virtualness from."""
        assert _is_virtual_interim(self._invoice(None)) is False

    def test_zero_vat_invoice_is_not_interim(self, virtual_event: Event) -> None:
        """No VAT charged means nothing to disclose."""
        assert _is_virtual_interim(self._invoice(virtual_event, total_vat=Decimal("0.00"))) is False

    def test_missing_buyer_country_is_not_interim(self, virtual_event: Event) -> None:
        """An empty buyer country can't qualify as cross-border EU."""
        assert _is_virtual_interim(self._invoice(virtual_event, buyer_vat_country="")) is False


# ---------------------------------------------------------------------------
# PDF context wiring
# ---------------------------------------------------------------------------


class TestVirtualInterimPdfContext:
    """generate_attendee_invoice passes is_virtual_interim into the PDF template."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_virtual_eu_b2c_invoice_renders_with_interim_flag(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """End-to-end: a virtual-event DE-buyer invoice renders with is_virtual_interim=True."""
        _make_org_invoicing_ready(organization)
        event.is_virtual = True
        event.save(update_fields=["is_virtual"])
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_interim_pdf",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_interim_pdf")

        assert invoice is not None
        assert _is_virtual_interim(invoice) is True
        mock_pdf.assert_called_once()
        _, context = mock_pdf.call_args.args
        assert context["is_virtual_interim"] is True

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_physical_invoice_renders_without_interim_flag(
        self,
        mock_pdf: MagicMock,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """The same DE buyer on a physical event renders without the interim wording."""
        _make_org_invoicing_ready(organization)
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_no_interim_pdf",
            buyer_billing_snapshot=_billing_snapshot(),
        )

        invoice = generate_attendee_invoice("cs_no_interim_pdf")

        assert invoice is not None
        mock_pdf.assert_called_once()
        _, context = mock_pdf.call_args.args
        assert context["is_virtual_interim"] is False
