"""Multi-tier ONLINE carts — per-tier ``reserve_batch_payments`` (#846 Task 8).

Before this task ``_online_checkout`` called ``reserve_batch_payments`` with a single
scalar ``tier`` (``locked_tiers[0]``), so a multi-tier ONLINE cart would stamp every
Payment row's VAT rate against the FIRST group's tier — money-wrong for every OTHER
tier in the cart. ``reserve_batch_payments`` now prices and VATs every ticket off its
own ``ticket.tier``, so a mixed-tier cart is correct end to end. Covers the four
scenarios from the task brief, service-level via ``BatchTicketService.create_batch()``.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService, CartGroup
from events.tasks.payments import cleanup_expired_payments

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    """Stripe-connected org with a non-trivial VAT rate (fallback for tiers with no override)."""
    organization.stripe_account_id = "acct_multitier"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.vat_country_code = "IT"
    organization.vat_rate = Decimal("22.00")
    organization.save()
    return organization


@pytest.fixture
def online_event(stripe_org: Organization) -> Event:
    """Future-dated public event on the Stripe-connected org."""
    return Event.objects.create(
        organization=stripe_org,
        name="Multi-Tier Online Event",
        slug="multi-tier-online-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=10,
    )


def _online_tier(event: Event, name: str, price: Decimal) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=price,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


def _online_pwyc_tier(event: Event, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("20.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("1.00"),
        total_quantity=100,
    )


@pytest.fixture
def tier_a(online_event: Event) -> TicketTier:
    return _online_tier(online_event, "Tier A", Decimal("50.00"))


@pytest.fixture
def tier_b(online_event: Event) -> TicketTier:
    return _online_tier(online_event, "Tier B", Decimal("30.00"))


class TestMultiTierReserveCreatesPerRowPayments:
    """Two ONLINE tiers, one checkout — each ticket's Payment mirrors its own tier."""

    def test_two_tiers_one_reservation_per_row_amounts_and_quantity_sold(
        self, online_event: Event, tier_a: TicketTier, tier_b: TicketTier, member_user: RevelUser
    ) -> None:
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(
                tier=tier_b,
                items=[TicketPurchaseItem(guest_name="Bob"), TicketPurchaseItem(guest_name="Cee")],
            ),
        ]

        result = BatchTicketService(online_event, user=member_user, groups=groups).create_batch()

        assert isinstance(result, tuple)
        tickets, reservation_id = result
        assert isinstance(reservation_id, UUID)
        assert len(tickets) == 3
        assert all(tk.status == Ticket.TicketStatus.PENDING for tk in tickets)

        payments = list(Payment.objects.filter(reservation_id=reservation_id).select_related("ticket__tier"))
        assert len(payments) == 3
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
        # Every Payment row's amount matches its OWN ticket's tier price — not a
        # scalar stamped from the first group's tier.
        for payment in payments:
            assert payment.amount == payment.ticket.tier.price

        tier_a.refresh_from_db()
        tier_b.refresh_from_db()
        assert tier_a.quantity_sold == 1
        assert tier_b.quantity_sold == 2


class TestMultiTierPerTierVatRate:
    """A tier-level ``vat_rate`` override must not leak onto a sibling tier's rows.

    This pins the per-tier memoization in ``_resolve_ticket_amounts`` (cache keyed by
    ``(tier.pk, price)``): the old scalar-tier code computed ONE ``fallback_vat_rate``
    from ``locked_tiers[0]`` for the whole cart, so tier B's rows would have carried
    tier A's 10% override instead of the org's 22% fallback. This test would FAIL
    against that code (tier B's rows would show 10.00, not 22.00).
    """

    def test_each_tier_effective_vat_rate_lands_on_its_own_rows(
        self, online_event: Event, tier_a: TicketTier, tier_b: TicketTier, member_user: RevelUser
    ) -> None:
        tier_a.vat_rate = Decimal("10.00")
        tier_a.save(update_fields=["vat_rate"])
        assert tier_b.vat_rate is None  # falls back to the org's 22.00

        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        result = BatchTicketService(online_event, user=member_user, groups=groups).create_batch()

        assert isinstance(result, tuple)
        _, reservation_id = result
        payments = list(Payment.objects.filter(reservation_id=reservation_id).select_related("ticket__tier"))
        for payment in payments:
            expected_rate = Decimal("10.00") if payment.ticket.tier_id == tier_a.id else Decimal("22.00")
            assert payment.vat_rate == expected_rate


class TestMultiTierZeroedCartReroute:
    """A buyer-zeroed multi-tier cart reroutes to free; a mixed one stays paid."""

    def test_two_pwyc_groups_both_zero_reroutes_to_free(self, online_event: Event, member_user: RevelUser) -> None:
        """Every line in every group is zero -> ACTIVE tickets, no Payment rows at all."""
        pwyc_a = _online_pwyc_tier(online_event, "PWYC A")
        pwyc_b = _online_pwyc_tier(online_event, "PWYC B")
        # Relax the buyer-facing floor below full_clean's MinValueValidator(1) via
        # .update() (bypasses TimeStampedModel.save's full_clean) so a 0.00 PWYC
        # amount is accepted by _assert_pwyc_amount.
        TicketTier.objects.filter(pk__in=[pwyc_a.pk, pwyc_b.pk]).update(pwyc_min=Decimal("0.00"))
        pwyc_a.refresh_from_db()
        pwyc_b.refresh_from_db()

        groups = [
            CartGroup(tier=pwyc_a, items=[TicketPurchaseItem(guest_name="Ann")], pwyc_amount=Decimal("0.00")),
            CartGroup(tier=pwyc_b, items=[TicketPurchaseItem(guest_name="Bob")], pwyc_amount=Decimal("0.00")),
        ]

        result = BatchTicketService(online_event, user=member_user, groups=groups).create_batch()

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(tk.status == Ticket.TicketStatus.ACTIVE for tk in result)
        assert not Payment.objects.filter(ticket__in=result).exists()

    def test_pwyc_zero_mixed_with_positive_fixed_tier_stays_paid(
        self, online_event: Event, tier_a: TicketTier, member_user: RevelUser
    ) -> None:
        """One zeroed group + one positive group -> paid path, every ticket keeps its 1:1 Payment row."""
        pwyc_tier = _online_pwyc_tier(online_event, "PWYC")
        TicketTier.objects.filter(pk=pwyc_tier.pk).update(pwyc_min=Decimal("0.00"))
        pwyc_tier.refresh_from_db()

        groups = [
            CartGroup(tier=pwyc_tier, items=[TicketPurchaseItem(guest_name="Ann")], pwyc_amount=Decimal("0.00")),
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        result = BatchTicketService(online_event, user=member_user, groups=groups).create_batch()

        assert isinstance(result, tuple)
        tickets, reservation_id = result
        assert len(tickets) == 2
        payments = list(Payment.objects.filter(reservation_id=reservation_id).select_related("ticket"))
        # 1:1 ticket<->Payment pairing, including the zero-amount row.
        assert {p.ticket_id for p in payments} == {tk.id for tk in tickets}
        zero_payment = next(p for p in payments if p.ticket.tier_id == pwyc_tier.id)
        positive_payment = next(p for p in payments if p.ticket.tier_id == tier_a.id)
        assert zero_payment.amount == Decimal("0.00")
        assert positive_payment.amount == Decimal("50.00")


class TestMultiTierReclaimSymmetry:
    """The expiry sweep must release BOTH tiers' capacity, each by its own count."""

    def test_expiring_a_two_tier_reservation_restores_each_tiers_quantity_sold(
        self, online_event: Event, tier_a: TicketTier, tier_b: TicketTier, member_user: RevelUser
    ) -> None:
        pre_a, pre_b = tier_a.quantity_sold, tier_b.quantity_sold
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(
                tier=tier_b,
                items=[TicketPurchaseItem(guest_name="Bob"), TicketPurchaseItem(guest_name="Cee")],
            ),
        ]

        result = BatchTicketService(online_event, user=member_user, groups=groups).create_batch()
        assert isinstance(result, tuple)
        _, reservation_id = result

        tier_a.refresh_from_db()
        tier_b.refresh_from_db()
        assert tier_a.quantity_sold == pre_a + 1
        assert tier_b.quantity_sold == pre_b + 2

        # Force-expire the reservation and run the reclaim sweep (mirrors
        # test_batch_ticket_service_reserve.py's sibling reserve tests).
        Payment.objects.filter(reservation_id=reservation_id).update(expires_at=timezone.now() - timedelta(minutes=1))
        released = cleanup_expired_payments()
        assert released == 3

        tier_a.refresh_from_db()
        tier_b.refresh_from_db()
        assert tier_a.quantity_sold == pre_a
        assert tier_b.quantity_sold == pre_b
