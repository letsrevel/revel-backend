"""Sibling-payment grouping by reservation_id, not stripe_session_id (#632).

A reserved-but-not-sessioned batch has stripe_session_id="" — grouping siblings by
it would wrongly collide unrelated reservations for the same user/tier. Grouping
must use reservation_id instead (falling back to stripe_session_id for legacy rows
that predate the backfill).
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import stripe_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def paid_ticket_tier(event: Event, stripe_connected_organization: Organization) -> TicketTier:
    """A paid ticket tier on a Stripe-connected event."""
    event.organization = stripe_connected_organization
    event.save()
    ga_tier = event.ticket_tiers.first()
    assert ga_tier is not None
    ga_tier.price = Decimal("25.00")
    ga_tier.total_quantity = 10
    ga_tier.save()
    return ga_tier


def _make_ticket(event: Event, tier: TicketTier, user: RevelUser, guest_name: str = "A") -> Ticket:
    return Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING, guest_name=guest_name
    )


def _reserve(event: Event, tier: TicketTier, user: RevelUser) -> Payment:
    """Reserve one ticket (un-sessioned Payment row) and return it."""
    ticket = _make_ticket(event, tier, user)
    reservation_id = uuid4()
    stripe_service.reserve_batch_payments(
        event=event, tier=tier, user=user, tickets=[ticket], reservation_id=reservation_id
    )
    payment = Payment.objects.get(ticket=ticket)
    assert payment.stripe_session_id == ""
    assert payment.reservation_id == reservation_id
    return payment


class TestReservationScopedGrouping:
    def test_cancel_scopes_to_reservation_not_empty_session(
        self, event: Event, paid_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Two independent un-sessioned reservations share stripe_session_id="" but have
        distinct reservation_id. Cancelling one must not touch the other's rows — the old
        stripe_session_id-based grouping would collide them both under "" and delete both.
        """
        payment_a = _reserve(event, paid_ticket_tier, member_user)
        payment_b = _reserve(event, paid_ticket_tier, member_user)
        ticket_a_id = payment_a.ticket_id
        ticket_b_id = payment_b.ticket_id

        cancelled = stripe_service.cancel_pending_checkout(str(payment_a.id), member_user)

        assert cancelled == 1
        assert not Payment.objects.filter(pk=payment_a.pk).exists()
        assert not Ticket.objects.filter(pk=ticket_a_id).exists()
        # Reservation B is untouched.
        assert Payment.objects.filter(pk=payment_b.pk, status=Payment.PaymentStatus.PENDING).exists()
        assert Ticket.objects.filter(pk=ticket_b_id, status=Ticket.TicketStatus.PENDING).exists()

    def test_cleanup_expired_batch_scopes_to_reservation_not_empty_session(
        self, event: Event, paid_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Same scoping guarantee for the resume-checkout expiry cleanup path."""
        payment_a = _reserve(event, paid_ticket_tier, member_user)
        payment_b = _reserve(event, paid_ticket_tier, member_user)
        ticket_b_id = payment_b.ticket_id

        stripe_service._cleanup_expired_batch(payment_a)

        assert not Payment.objects.filter(pk=payment_a.pk).exists()
        assert Payment.objects.filter(pk=payment_b.pk, status=Payment.PaymentStatus.PENDING).exists()
        assert Ticket.objects.filter(pk=ticket_b_id, status=Ticket.TicketStatus.PENDING).exists()
