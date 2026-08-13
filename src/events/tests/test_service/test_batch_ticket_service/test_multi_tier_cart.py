"""Multi-group carts end to end — the cart engine's happy paths and invariants (#846).

``create_batch`` no longer refuses a cart spanning several tiers: every per-tier step
(eligibility, capacity, seats, pricing, stamping, ticket writing, ``quantity_sold``)
runs per group, while the cart-level ones (event capacity, per-user caps, discount
usage, the waitlist claim) run once over the whole cart.

ONLINE carts stay single-tier until ``reserve_batch_payments`` goes per-tier, so
everything here uses free / offline tiers.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import DiscountCode, Event, Organization, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService, CartGroup

pytestmark = pytest.mark.django_db


def _free_tier(event: Event, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        total_quantity=100,
    )


def _offline_tier(
    event: Event, name: str, *, price: Decimal = Decimal("20.00"), total_quantity: int | None = 100
) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=price,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=total_quantity,
    )


@pytest.fixture
def free_tier_a(batch_event: Event) -> TicketTier:
    return _free_tier(batch_event, "Free A")


@pytest.fixture
def free_tier_b(batch_event: Event) -> TicketTier:
    return _free_tier(batch_event, "Free B")


@pytest.fixture
def offline_fixed_tier(batch_event: Event) -> TicketTier:
    return _offline_tier(batch_event, "Fixed")


@pytest.fixture
def offline_pwyc_tier(batch_event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=batch_event,
        name="PWYC",
        price=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5.00"),
        pwyc_max=Decimal("50.00"),
        total_quantity=100,
    )


class TestMultiTierCart:
    """Two tiers, one checkout."""

    def test_free_cart_two_tiers(
        self, batch_event: Event, free_tier_a: TicketTier, free_tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """Every group is written, and each tier's ``quantity_sold`` moves by its own count."""
        groups = [
            CartGroup(tier=free_tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(
                tier=free_tier_b,
                items=[TicketPurchaseItem(guest_name="Bob"), TicketPurchaseItem(guest_name="Cee")],
            ),
        ]

        result = BatchTicketService(batch_event, user=batch_user, groups=groups).create_batch()

        assert isinstance(result, list)
        assert len(result) == 3
        assert {ticket.tier_id for ticket in result} == {free_tier_a.id, free_tier_b.id}
        assert all(ticket.status == Ticket.TicketStatus.ACTIVE for ticket in result)
        free_tier_a.refresh_from_db()
        free_tier_b.refresh_from_db()
        assert (free_tier_a.quantity_sold, free_tier_b.quantity_sold) == (1, 2)

    def test_pwyc_and_fixed_coexist(
        self,
        batch_event: Event,
        offline_fixed_tier: TicketTier,
        offline_pwyc_tier: TicketTier,
        batch_user: RevelUser,
    ) -> None:
        """Pricing and the ``price_paid`` decision are per group, not per cart (spec §5.5)."""
        groups = [
            CartGroup(tier=offline_fixed_tier, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(
                tier=offline_pwyc_tier,
                items=[TicketPurchaseItem(guest_name="Bob")],
                pwyc_amount=Decimal("25.00"),
            ),
        ]

        result = BatchTicketService(batch_event, user=batch_user, groups=groups).create_batch()

        assert isinstance(result, list)
        pwyc_ticket = next(ticket for ticket in result if ticket.tier_id == offline_pwyc_tier.id)
        assert pwyc_ticket.price_paid == Decimal("25.00")  # PWYC stamps
        fixed_ticket = next(ticket for ticket in result if ticket.tier_id == offline_fixed_tier.id)
        assert fixed_ticket.price_paid is None  # flat tier: NULL claim holds

    def test_tier_oversell_bounded_per_tier(
        self, batch_event: Event, offline_fixed_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        """One group over its tier's cap fails the WHOLE cart — no partial write."""
        capped_tier = _offline_tier(batch_event, "Capped", total_quantity=1)
        groups = [
            CartGroup(
                tier=capped_tier,
                items=[TicketPurchaseItem(guest_name="Ann"), TicketPurchaseItem(guest_name="Bob")],
            ),
            CartGroup(tier=offline_fixed_tier, items=[TicketPurchaseItem(guest_name="Cee")]),
        ]

        with pytest.raises(HttpError) as exc_info:
            BatchTicketService(batch_event, user=batch_user, groups=groups).create_batch()

        assert exc_info.value.status_code == 400
        assert Ticket.objects.filter(event=batch_event).count() == 0
        capped_tier.refresh_from_db()
        offline_fixed_tier.refresh_from_db()
        assert (capped_tier.quantity_sold, offline_fixed_tier.quantity_sold) == (0, 0)

    def test_event_capacity_counts_whole_cart(self, organization: Organization, batch_user: RevelUser) -> None:
        """The event cap sees the cart's total, not each group in isolation: 2 + 2 > 3."""
        event = Event.objects.create(
            organization=organization,
            name="Tiny Event",
            slug="tiny-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=3,
            max_tickets_per_user=10,  # out of the way: the event CAPACITY is what's under test
        )
        groups = [
            CartGroup(
                tier=_free_tier(event, "Free A"),
                items=[TicketPurchaseItem(guest_name="Ann"), TicketPurchaseItem(guest_name="Bob")],
            ),
            CartGroup(
                tier=_free_tier(event, "Free B"),
                items=[TicketPurchaseItem(guest_name="Cee"), TicketPurchaseItem(guest_name="Dee")],
            ),
        ]

        with pytest.raises(HttpError) as exc_info:
            BatchTicketService(event, user=batch_user, groups=groups).create_batch()

        assert exc_info.value.status_code == 400
        assert "Only 3 spot(s) remaining" in str(exc_info.value.message)
        assert Ticket.objects.filter(event=event).count() == 0


class TestDiscountScoping:
    """A cart's code applies to the groups it was validated for — and only those."""

    @pytest.fixture
    def code(self, organization: Organization) -> DiscountCode:
        """10% off — 20.00 → 18.00 (‑2.00)."""
        return DiscountCode.objects.create(
            code="PCT10",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            currency="EUR",
            max_uses_per_user=10,
        )

    def test_discount_applies_only_to_valid_tiers(
        self, batch_event: Event, batch_user: RevelUser, code: DiscountCode
    ) -> None:
        """Tier A is discounted and stamps; tier B is untouched and keeps its NULL claim."""
        tier_a = _offline_tier(batch_event, "Tier A")
        tier_b = _offline_tier(batch_event, "Tier B")
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        result = BatchTicketService(
            batch_event,
            user=batch_user,
            discount_code=code,
            groups=groups,
            discount_valid_tier_ids={tier_a.id},
        ).create_batch()

        assert isinstance(result, list)
        ticket_a = next(ticket for ticket in result if ticket.tier_id == tier_a.id)
        ticket_b = next(ticket for ticket in result if ticket.tier_id == tier_b.id)
        assert (ticket_a.price_paid, ticket_a.discount_amount, ticket_a.discount_code_id) == (
            Decimal("18.00"),
            Decimal("2.00"),
            code.id,
        )
        assert (ticket_b.price_paid, ticket_b.discount_amount, ticket_b.discount_code_id) == (None, None, None)

    def test_min_purchase_amount_sums_only_the_applicable_groups(
        self, batch_event: Event, batch_user: RevelUser, code: DiscountCode
    ) -> None:
        """The threshold measures what the code can actually discount — 20.00, not 50.00."""
        code.min_purchase_amount = Decimal("40.00")
        code.save(update_fields=["min_purchase_amount"])
        tier_a = _offline_tier(batch_event, "Tier A")
        tier_b = _offline_tier(batch_event, "Tier B", price=Decimal("30.00"))
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        with pytest.raises(HttpError) as exc_info:
            BatchTicketService(
                batch_event,
                user=batch_user,
                discount_code=code,
                groups=groups,
                discount_valid_tier_ids={tier_a.id},
            ).create_batch()

        assert exc_info.value.status_code == 400
        assert "Minimum purchase amount" in str(exc_info.value.message)
        assert Ticket.objects.filter(event=batch_event).count() == 0
