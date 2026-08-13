"""Attendee invoice — mixed-cart coverage (#846 Task 11).

Pins how ``generate_attendee_invoice`` handles a multi-tier ONLINE cart: each
Payment row already carries its OWN ticket's tier (``payment.ticket.tier``),
so ``_build_line_items`` builds one description/rate/amount per row rather
than a cart-wide scalar. This test builds a real mixed cart through
``BatchTicketService`` (reusing the #846 Task 8 per-tier reserve plumbing —
see ``test_service/test_batch_ticket_service/test_multi_tier_online.py``),
forces the resulting Payment rows SUCCEEDED with a session id (mirroring the
webhook), and asserts the invoice the service builds from them.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.schema import TicketPurchaseItem
from events.schema.ticket import BuyerBillingInfoSchema
from events.service.attendee_invoice_service import generate_attendee_invoice
from events.service.batch_ticket_service import BatchTicketService, CartGroup

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"


@pytest.fixture
def invoicing_org(organization: Organization) -> Organization:
    """Stripe-connected, invoicing-ready org with a non-trivial fallback VAT rate."""
    organization.stripe_account_id = "acct_mixed_cart"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.vat_country_code = "IT"
    organization.vat_id = "IT12345678901"
    organization.vat_id_validated = True
    organization.vat_rate = Decimal("22.00")
    organization.billing_name = "ACME SRL"
    organization.billing_address = "Via Roma 1, 00100 Roma"
    organization.billing_email = "billing@acme.it"
    organization.contact_email = "info@acme.it"
    organization.invoicing_mode = Organization.InvoicingMode.HYBRID
    organization.save()
    return organization


@pytest.fixture
def mixed_event(invoicing_org: Organization) -> Event:
    """Future-dated public physical event on the invoicing-ready org."""
    return Event.objects.create(
        organization=invoicing_org,
        name="Mixed Cart Event",
        slug="mixed-cart-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=10,
    )


@pytest.fixture
def tier_standard(mixed_event: Event) -> TicketTier:
    """No tier-level VAT override -- falls back to the org's 22.00% rate."""
    return TicketTier.objects.create(
        event=mixed_event,
        name="Standard",
        price=Decimal("50.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


@pytest.fixture
def tier_vip(mixed_event: Event) -> TicketTier:
    """Tier-level VAT rate override (10.00%), distinct from the org's 22.00%."""
    tier = TicketTier.objects.create(
        event=mixed_event,
        name="VIP",
        price=Decimal("120.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )
    tier.vat_rate = Decimal("10.00")
    tier.save(update_fields=["vat_rate"])
    return tier


class TestMixedCartAttendeeInvoice:
    """Two ONLINE tiers, one checkout session -> one invoice with per-tier line items."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_line_items_reflect_each_payments_own_tier(
        self,
        mock_pdf: t.Any,
        mixed_event: Event,
        tier_standard: TicketTier,
        tier_vip: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """One line item per Payment row; each carries ITS ticket's own tier name/rate/amount."""
        billing_info = BuyerBillingInfoSchema(  # type: ignore[call-arg]
            billing_name="Buyer GmbH", billing_email="buyer@example.de"
        )
        groups = [
            CartGroup(tier=tier_standard, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_vip, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]
        result = BatchTicketService(mixed_event, user=member_user, groups=groups).create_batch(
            billing_info=billing_info
        )
        assert isinstance(result, tuple)
        tickets, reservation_id = result
        assert len(tickets) == 2

        # Mirror the webhook: mark both PENDING Payment rows SUCCEEDED under one
        # Stripe session id (no real Stripe/network call needed for this test).
        session_id = "cs_mixed_cart_1"
        updated = Payment.objects.filter(reservation_id=reservation_id).update(
            status=Payment.PaymentStatus.SUCCEEDED, stripe_session_id=session_id
        )
        assert updated == 2

        invoice = generate_attendee_invoice(session_id)
        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT  # HYBRID mode

        payments = list(
            Payment.objects.filter(reservation_id=reservation_id).select_related("ticket__tier").order_by("created_at")
        )
        assert len(payments) == 2
        standard_payment = next(p for p in payments if p.ticket.tier_id == tier_standard.id)
        vip_payment = next(p for p in payments if p.ticket.tier_id == tier_vip.id)

        # One line item per Payment row.
        assert len(invoice.line_items) == 2

        # Each line's description names ITS OWN ticket's tier -- never the sibling's.
        standard_line = next(li for li in invoice.line_items if "Ann" in li["description"])
        vip_line = next(li for li in invoice.line_items if "Bob" in li["description"])
        assert tier_standard.name in standard_line["description"]
        assert tier_vip.name not in standard_line["description"]
        assert tier_vip.name in vip_line["description"]
        assert tier_standard.name not in vip_line["description"]

        # Each line carries its OWN payment's amount and VAT rate -- not a
        # cart-wide scalar stamped from whichever tier locked first.
        assert standard_payment.vat_rate == Decimal("22.00")  # fallback to org rate
        assert vip_payment.vat_rate == Decimal("10.00")  # tier-level override
        assert Decimal(standard_line["unit_price_gross"]) == standard_payment.amount == Decimal("50.00")
        assert Decimal(vip_line["unit_price_gross"]) == vip_payment.amount == Decimal("120.00")
        assert Decimal(standard_line["vat_rate"]) == standard_payment.vat_rate
        assert Decimal(vip_line["vat_rate"]) == vip_payment.vat_rate

        # Header totals are sums over the Payment rows.
        assert invoice.total_gross == standard_payment.amount + vip_payment.amount == Decimal("170.00")
        assert invoice.total_net == (standard_payment.net_amount or Decimal("0.00")) + (
            vip_payment.net_amount or Decimal("0.00")
        )
        assert invoice.total_vat == (standard_payment.vat_amount or Decimal("0.00")) + (
            vip_payment.vat_amount or Decimal("0.00")
        )

        # Header `vat_rate` is intentionally a scalar, documented in
        # generate_attendee_invoice as "Dominant VAT rate (from first payment)"
        # -- unlike the per-row line_items, it does NOT attempt to represent a
        # mixed-rate cart. Pin that it really is the first (by created_at)
        # payment's rate, whichever tier that happens to be -- the per-tier
        # rates the two lines above show are the authoritative breakdown; the
        # header is a display simplification only.
        assert invoice.vat_rate == payments[0].vat_rate
