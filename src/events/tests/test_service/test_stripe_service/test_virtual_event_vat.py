"""Tests for the virtual-event VAT plumbing in the Stripe checkout flow (#869).

``_resolve_ticket_amounts(..., is_virtual=True)`` enables the reverse-charge /
non-EU branches of ``determine_attendee_vat``; ``reserve_batch_payments`` passes
``event.is_virtual`` through, so a reverse-charged virtual buyer's Payment rows
are created at the net amount.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import stripe_service
from events.service.attendee_vat_service import BuyerVATContext
from events.service.stripe_service import _resolve_ticket_amounts

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """An IT org with Stripe connected and a 22% VAT rate."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.vat_country_code = "IT"
    organization.vat_rate = Decimal("22.00")
    organization.save()
    return organization


@pytest.fixture
def paid_ticket_tier(event: Event, stripe_connected_organization: Organization) -> TicketTier:
    """A paid tier (gross 12.20 -> net 10.00 at 22%) on the Stripe-connected event."""
    ga_tier = event.ticket_tiers.first()
    assert ga_tier is not None
    ga_tier.price = Decimal("12.20")
    ga_tier.total_quantity = 10
    ga_tier.save()
    return ga_tier


EU_B2B_CONTEXT = BuyerVATContext(buyer_country="DE", buyer_vat_validated=True)


class TestResolveTicketAmountsVirtual:
    """_resolve_ticket_amounts branches on is_virtual."""

    def test_virtual_eu_b2b_validated_yields_net_reverse_charge(
        self, paid_ticket_tier: TicketTier, stripe_connected_organization: Organization
    ) -> None:
        """Virtual + cross-border EU B2B with validated VAT ID -> net price, RC."""
        amounts, buyer_vat_validated = _resolve_ticket_amounts(
            [(Decimal("12.20"), paid_ticket_tier)],
            org=stripe_connected_organization,
            buyer_vat_context=EU_B2B_CONTEXT,
            is_virtual=True,
        )

        assert buyer_vat_validated is True
        assert len(amounts) == 1
        assert amounts[0].effective_price == Decimal("10.00")
        assert amounts[0].net_amount == Decimal("10.00")
        assert amounts[0].vat_amount == Decimal("0.00")
        assert amounts[0].vat_rate == Decimal("0.00")
        assert amounts[0].reverse_charge is True

    def test_physical_same_context_yields_gross(
        self, paid_ticket_tier: TicketTier, stripe_connected_organization: Organization
    ) -> None:
        """The identical buyer context on a physical event pays gross at 22% (#868)."""
        amounts, buyer_vat_validated = _resolve_ticket_amounts(
            [(Decimal("12.20"), paid_ticket_tier)],
            org=stripe_connected_organization,
            buyer_vat_context=EU_B2B_CONTEXT,
            is_virtual=False,
        )

        assert buyer_vat_validated is True
        assert amounts[0].effective_price == Decimal("12.20")
        assert amounts[0].net_amount == Decimal("10.00")
        assert amounts[0].vat_amount == Decimal("2.20")
        assert amounts[0].vat_rate == Decimal("22.00")
        assert amounts[0].reverse_charge is False

    def test_virtual_non_eu_buyer_yields_net_without_reverse_charge(
        self, paid_ticket_tier: TicketTier, stripe_connected_organization: Organization
    ) -> None:
        """Virtual + non-EU buyer -> net price, no VAT, no reverse charge."""
        amounts, _ = _resolve_ticket_amounts(
            [(Decimal("12.20"), paid_ticket_tier)],
            org=stripe_connected_organization,
            buyer_vat_context=BuyerVATContext(buyer_country="US", buyer_vat_validated=False),
            is_virtual=True,
        )

        assert amounts[0].effective_price == Decimal("10.00")
        assert amounts[0].vat_amount == Decimal("0.00")
        assert amounts[0].reverse_charge is False

    def test_virtual_eu_b2c_yields_gross(
        self, paid_ticket_tier: TicketTier, stripe_connected_organization: Organization
    ) -> None:
        """Virtual + cross-border EU B2C -> gross at the seller's rate (interim)."""
        amounts, _ = _resolve_ticket_amounts(
            [(Decimal("12.20"), paid_ticket_tier)],
            org=stripe_connected_organization,
            buyer_vat_context=BuyerVATContext(buyer_country="DE", buyer_vat_validated=False),
            is_virtual=True,
        )

        assert amounts[0].effective_price == Decimal("12.20")
        assert amounts[0].vat_amount == Decimal("2.20")
        assert amounts[0].reverse_charge is False


class TestReserveBatchPaymentsVirtual:
    """reserve_batch_payments passes event.is_virtual into the amount resolution."""

    @staticmethod
    def _reserve(event: Event, tier: TicketTier, user: RevelUser) -> Payment:
        ticket = Ticket.objects.create(
            event=event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING, guest_name="A"
        )
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event,
            user=user,
            tickets=[ticket],
            reservation_id=rid,
            buyer_vat_context=EU_B2B_CONTEXT,
        )
        return Payment.objects.get(reservation_id=rid)

    def test_virtual_event_rc_buyer_pays_net(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """On a virtual event the RC buyer's Payment row is created at the net amount."""
        event.is_virtual = True
        event.save(update_fields=["is_virtual"])

        payment = self._reserve(event, paid_ticket_tier, organization_owner_user)

        assert payment.amount == Decimal("10.00")
        assert payment.net_amount == Decimal("10.00")
        assert payment.vat_amount == Decimal("0.00")
        assert payment.vat_rate == Decimal("0.00")

    def test_physical_event_same_buyer_pays_gross(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """On the physical event the same buyer context is charged the gross amount."""
        assert event.is_virtual is False

        payment = self._reserve(event, paid_ticket_tier, organization_owner_user)

        assert payment.amount == Decimal("12.20")
        assert payment.net_amount == Decimal("10.00")
        assert payment.vat_amount == Decimal("2.20")
        assert payment.vat_rate == Decimal("22.00")
