"""Shared helpers for the attendee-invoice tests."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.models.ticket import Payment
from events.service.attendee_invoice_draft_service import (
    DERIVED_TOTAL_FIELDS,
    delete_draft_invoice,
    issue_draft_invoice,
    update_draft_invoice,
)
from events.service.attendee_invoice_service import (
    _is_export,
    generate_attendee_invoice,
    set_invoicing_mode,
    validate_invoicing_prerequisites,
)


MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"
MOCK_SEND_EMAIL = "common.tasks.send_email"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_org_invoicing_ready(org: Organization) -> Organization:
    """Configure an org with all prerequisites for invoicing."""
    org.vat_country_code = "IT"
    org.vat_id = "IT12345678901"
    org.vat_id_validated = True
    org.vat_rate = Decimal("22.00")
    org.billing_name = "ACME SRL"
    org.billing_address = "Via Roma 1, 00100 Roma"
    org.billing_email = "billing@acme.it"
    org.contact_email = "info@acme.it"
    org.save()
    return org


def _create_payment(
    *,
    user: RevelUser,
    event: Event,
    tier: TicketTier,
    session_id: str = "cs_test_123",
    amount: Decimal = Decimal("100.00"),
    net_amount: Decimal | None = Decimal("81.97"),
    vat_amount: Decimal | None = Decimal("18.03"),
    vat_rate: Decimal | None = Decimal("22.00"),
    buyer_billing_snapshot: dict[str, t.Any] | None = None,
    guest_name: str = "Test Guest",
) -> Payment:
    """Create a ticket and payment for testing."""
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, guest_name=guest_name)
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session_id,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=amount,
        net_amount=net_amount,
        vat_amount=vat_amount,
        vat_rate=vat_rate,
        platform_fee=Decimal("5.00"),
        currency="EUR",
        buyer_billing_snapshot=buyer_billing_snapshot,
    )


def _default_billing_snapshot(reverse_charge: bool = False) -> dict[str, t.Any]:
    return {
        "billing_name": "Buyer GmbH",
        "vat_id": "DE123456789",
        "vat_country_code": "DE",
        "vat_id_validated": True,
        "billing_address": "Berliner Str. 1, 10115 Berlin",
        "billing_email": "buyer@example.de",
        "reverse_charge": reverse_charge,
    }


_INVOICE_COUNTER = 0


def _create_draft(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """Create a minimal draft invoice for testing."""
    global _INVOICE_COUNTER  # noqa: PLW0603
    _INVOICE_COUNTER += 1
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_draft_{_INVOICE_COUNTER}",
        invoice_number=f"TEST-{_INVOICE_COUNTER:06d}",
        status=AttendeeInvoice.InvoiceStatus.DRAFT,
        total_gross=Decimal("100.00"),
        total_net=Decimal("81.97"),
        total_vat=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        currency="EUR",
        # Reconciles with the totals above: since #911 an invoice whose header
        # disagrees with its lines cannot be issued, and a fixture that drifts
        # is not a shape production can produce.
        line_items=[
            {
                "description": "Event — Tier — Guest",
                "unit_price_gross": "100.00",
                "discount_amount": "0.00",
                "net_amount": "81.97",
                "vat_amount": "18.03",
                "vat_rate": "22.00",
            }
        ],
        seller_name="ACME SRL",
        seller_email="billing@acme.it",
        buyer_name="Original Buyer",
        buyer_email="buyer@example.com",
    )


def _create_issued(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """Create an issued invoice for testing."""
    inv = _create_draft(org, event, user)
    inv.status = AttendeeInvoice.InvoiceStatus.ISSUED
    inv.issued_at = timezone.now()
    inv.save()
    return inv
