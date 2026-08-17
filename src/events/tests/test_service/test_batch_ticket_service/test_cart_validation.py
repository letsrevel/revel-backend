"""Cart-shape validation — the rules ``create_batch`` applies to the cart as a whole (#846).

One test per rule in ``BatchTicketService._validate_cart``, all driven through the
cart-form ``create_batch()`` so the checks are pinned where they are authoritative:
in the service, not in whichever controller happens to call it. The exact wording is
asserted because the frontend surfaces these strings to the buyer.
"""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.models.organization import Organization
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService, CartGroup
from events.service.batch_ticket_service.context import validate_cart_shape

pytestmark = pytest.mark.django_db


def _tier(
    event: Event,
    name: str,
    *,
    currency: str = "EUR",
    method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.OFFLINE,
    price: Decimal = Decimal("20.00"),
) -> TicketTier:
    """A plain GA tier — the validation rules never get as far as seats or capacity."""
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=price,
        currency=currency,
        payment_method=method,
        total_quantity=100,
    )


@pytest.fixture
def tier_a(batch_event: Event) -> TicketTier:
    return _tier(batch_event, "Tier A")


@pytest.fixture
def tier_b(batch_event: Event) -> TicketTier:
    return _tier(batch_event, "Tier B", price=Decimal("30.00"))


@pytest.fixture
def pwyc_tier(batch_event: Event) -> TicketTier:
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


def _run(event: Event, user: RevelUser, groups: list[CartGroup]) -> HttpError:
    """Drive the cart through ``create_batch`` and return the raised ``HttpError``."""
    service = BatchTicketService(event, user=user, groups=groups)
    with pytest.raises(HttpError) as exc_info:
        service.create_batch()
    assert exc_info.value.status_code == 400
    return exc_info.value


class TestCartShape:
    """Duplicate tiers and the uniformity rules."""

    def test_empty_cart_is_rejected(self) -> None:
        """An empty cart is a buyer 400, not an IndexError downstream.

        Called directly: the cart-form service constructor already refuses
        ``groups=[]`` with a TypeError, so this rule can only be reached by the
        guest pre-branch, which runs ``validate_cart_shape`` on its own.
        """
        with pytest.raises(HttpError) as exc_info:
            validate_cart_shape([])

        assert exc_info.value.status_code == 400
        assert str(exc_info.value.message) == "Your cart is empty."

    def test_duplicate_tier_is_rejected(self, batch_event: Event, tier_a: TicketTier, batch_user: RevelUser) -> None:
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "Each tier may appear only once per checkout."

    def test_mixed_currency_is_rejected(self, batch_event: Event, tier_a: TicketTier, batch_user: RevelUser) -> None:
        usd_tier = _tier(batch_event, "Tier USD", currency="USD")
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=usd_tier, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "All tickets in one checkout must use the same currency."

    def test_mixed_payment_method_is_rejected(
        self, batch_event: Event, tier_a: TicketTier, batch_user: RevelUser
    ) -> None:
        door_tier = _tier(batch_event, "Tier Door", method=TicketTier.PaymentMethod.AT_THE_DOOR)
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=door_tier, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "All tickets in one checkout must use the same payment method."


class TestLockedCartUniformity:
    """The uniformity rules are re-proven on the LOCKED rows, not just the stale read.

    An organizer can commit a tier edit between the controller's read and
    ``create_batch``'s ``select_for_update`` (the VIES round-trip deliberately sits in
    that window). Simulated here by editing the row *after* the groups are built: the
    in-memory tiers still agree, so ``_validate_cart`` passes and only the post-lock
    re-assert can catch it.
    """

    def test_currency_flipped_after_the_pre_lock_read_is_rejected(
        self, batch_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]
        TicketTier.objects.filter(pk=tier_b.pk).update(currency="USD")

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "All tickets in one checkout must use the same currency."
        assert Ticket.objects.filter(event=batch_event).count() == 0
        assert Payment.objects.filter(ticket__event=batch_event).count() == 0

    def test_payment_method_flipped_after_the_pre_lock_read_is_rejected(
        self, batch_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """The branch dispatch reads the anchor's method for the whole cart, so an
        unnoticed flip settles the sibling through the wrong path — worst case a
        now-ONLINE tier riding a free/offline checkout and losing its revenue.
        """
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]
        TicketTier.objects.filter(pk=tier_b.pk).update(payment_method=TicketTier.PaymentMethod.ONLINE)

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "All tickets in one checkout must use the same payment method."
        assert Ticket.objects.filter(event=batch_event).count() == 0
        assert Payment.objects.filter(ticket__event=batch_event).count() == 0


class TestPWYCPerGroup:
    """A PWYC amount belongs to exactly the groups whose tier is PWYC, within its bounds."""

    def test_pwyc_tier_without_an_amount_is_rejected(
        self, batch_event: Event, pwyc_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=pwyc_tier, items=[TicketPurchaseItem(guest_name="Ann")])]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "This tier requires a pay-what-you-can amount."

    def test_non_pwyc_tier_with_an_amount_is_rejected(
        self, batch_event: Event, tier_a: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")], pwyc_amount=Decimal("10.00"))]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "This tier does not accept a pay-what-you-can amount."

    def test_amount_below_the_tier_minimum_is_rejected(
        self, batch_event: Event, pwyc_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=pwyc_tier, items=[TicketPurchaseItem(guest_name="Ann")], pwyc_amount=Decimal("1.00"))]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "PWYC amount must be at least 5.00"

    def test_amount_above_the_tier_maximum_is_rejected(
        self, batch_event: Event, pwyc_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=pwyc_tier, items=[TicketPurchaseItem(guest_name="Ann")], pwyc_amount=Decimal("99.00"))]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "PWYC amount must be at most 50.00"

    def test_the_offending_group_is_found_even_when_another_group_is_fine(
        self, batch_event: Event, tier_a: TicketTier, pwyc_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        """Per-group, not per-cart: a valid fixed group does not excuse the PWYC one."""
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="Ann")]),
            CartGroup(tier=pwyc_tier, items=[TicketPurchaseItem(guest_name="Bob")]),
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "This tier requires a pay-what-you-can amount."


class TestDuplicateSeats:
    """The same seat can only be sold once — caught at payload level, before any lock."""

    @pytest.fixture
    def sector(self, organization: Organization, batch_event: Event) -> VenueSector:
        venue = Venue.objects.create(organization=organization, name="Hall", capacity=100)
        batch_event.venue = venue
        batch_event.save(update_fields=["venue"])
        return VenueSector.objects.create(venue=venue, name="Stalls")

    @pytest.fixture
    def seat(self, sector: VenueSector) -> VenueSeat:
        return VenueSeat.objects.create(
            sector=sector, label="A1", row_label="A", number=1, adjacency_index=0, is_active=True
        )

    def _uc_tier(self, batch_event: Event, sector: VenueSector, name: str) -> TicketTier:
        return TicketTier.objects.create(
            event=batch_event,
            name=name,
            price=Decimal("20.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            venue=sector.venue,
            sector=sector,
        )

    def test_two_groups_cannot_claim_the_same_seat(
        self, batch_event: Event, sector: VenueSector, seat: VenueSeat, batch_user: RevelUser
    ) -> None:
        """Without this rule both groups pass their own sector check and collide on the
        ``unique_ticket_event_seat`` constraint — a 500 where the buyer deserves a 400.
        """
        groups = [
            CartGroup(tier=self._uc_tier(batch_event, sector, "UC A"), items=[TicketPurchaseItem(seat_id=seat.id)]),
            CartGroup(tier=self._uc_tier(batch_event, sector, "UC B"), items=[TicketPurchaseItem(seat_id=seat.id)]),
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "The same seat cannot be purchased twice."
        assert Ticket.objects.filter(event=batch_event).count() == 0

    def test_one_group_cannot_claim_the_same_seat_twice(
        self, batch_event: Event, sector: VenueSector, seat: VenueSeat, batch_user: RevelUser
    ) -> None:
        groups = [
            CartGroup(
                tier=self._uc_tier(batch_event, sector, "UC A"),
                items=[TicketPurchaseItem(seat_id=seat.id), TicketPurchaseItem(seat_id=seat.id)],
            )
        ]

        error = _run(batch_event, batch_user, groups)

        assert str(error.message) == "The same seat cannot be purchased twice."
