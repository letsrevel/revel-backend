"""Guest checkout obeys the ``price_paid`` stamping rules (spec §5.5).

Guest checkout is one of the ticket-creating entrypoints enumerated in
``test_service/test_batch_ticket_service/test_price_paid_entrypoint_invariant.py``.
It funnels through ``BatchTicketService.create_batch`` exactly like authenticated
checkout — the online branch of ``handle_guest_ticket_checkout`` and the offline
``confirm_guest_action`` both go through it — so it must reach the single
``should_stamp_price_paid`` authority and never bypass it. These tests pin the
decisive case: on a category-priced offline tier the guest's ticket carries the
seat's resolved category price, and on a flat tier it stays NULL.
"""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, PriceCategory, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db

PREMIUM = Decimal("80.00")
STANDARD = Decimal("30.00")
FLAT = Decimal("50.00")


@pytest.fixture
def seated_venue(organization: Organization, guest_event_with_tickets: Event) -> tuple[Venue, VenueSector]:
    """Bind the guest-accessible event to a venue with one seated sector."""
    venue = Venue.objects.create(organization=organization, name="Guest Hall", capacity=100)
    sector = VenueSector.objects.create(venue=venue, name="Stalls")
    guest_event_with_tickets.venue = venue
    guest_event_with_tickets.save(update_fields=["venue"])
    return venue, sector


@pytest.fixture
def seats(seated_venue: tuple[Venue, VenueSector]) -> tuple[list[VenueSeat], PriceCategory, PriceCategory]:
    """A1 Premium, A2 Standard, A3 unpainted (falls back to the flat tier price)."""
    venue, sector = seated_venue
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
    standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#00aa00")
    painted: list[PriceCategory | None] = [premium, standard, None]
    seat_list = [
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{i + 1}",
            row_label="A",
            number=i + 1,
            adjacency_index=i,
            is_active=True,
            default_price_category=category,
        )
        for i, category in enumerate(painted)
    ]
    return seat_list, premium, standard


def _category_tier(
    event: Event, seated_venue: tuple[Venue, VenueSector], premium: PriceCategory, standard: PriceCategory
) -> TicketTier:
    venue, sector = seated_venue
    return TicketTier.objects.create(
        event=event,
        name="Category Stalls",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=venue,
        sector=sector,
        category_prices={str(premium.pk): str(PREMIUM), str(standard.pk): str(STANDARD)},
    )


def _flat_tier(event: Event, seated_venue: tuple[Venue, VenueSector]) -> TicketTier:
    venue, sector = seated_venue
    return TicketTier.objects.create(
        event=event,
        name="Flat Stalls",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=venue,
        sector=sector,
    )


def _confirm(user: RevelUser, event: Event, tier: TicketTier, seats: list[VenueSeat]) -> list[Ticket]:
    """Drive the offline guest flow's ticket-creating half: mint a token, then confirm it."""
    items = [schema.TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seat.pk) for i, seat in enumerate(seats)]
    token = guest_service.create_guest_ticket_token(user, event.id, tier.id, items)
    guest_service.confirm_guest_action(token)
    return list(Ticket.objects.filter(event=event, tier=tier).order_by("seat__adjacency_index"))


def test_guest_offline_confirm_stamps_category_price_per_seat(
    guest_event_with_tickets: Event,
    seated_venue: tuple[Venue, VenueSector],
    seats: tuple[list[VenueSeat], PriceCategory, PriceCategory],
    existing_guest_user: RevelUser,
) -> None:
    """A category-priced offline guest sale must record each seat's own price, not tier.price.

    A guest path that skipped ``should_stamp_price_paid`` would leave these NULL, and the
    money-bearing readers would then resolve the premium seat from ``tier.price`` — the exact
    silent mispricing the stamp exists to prevent.
    """
    seat_list, premium, standard = seats
    tier = _category_tier(guest_event_with_tickets, seated_venue, premium, standard)

    tickets = _confirm(existing_guest_user, guest_event_with_tickets, tier, seat_list)

    assert [t.price_paid for t in tickets] == [PREMIUM, STANDARD, FLAT]


def test_guest_offline_confirm_leaves_flat_tier_price_paid_null(
    guest_event_with_tickets: Event,
    seated_venue: tuple[Venue, VenueSector],
    seats: tuple[list[VenueSeat], PriceCategory, PriceCategory],
    existing_guest_user: RevelUser,
) -> None:
    """On a flat tier ``tier.price`` reconstructs the amount, so the NULL claim stays true."""
    seat_list, _premium, _standard = seats
    tier = _flat_tier(guest_event_with_tickets, seated_venue)

    tickets = _confirm(existing_guest_user, guest_event_with_tickets, tier, seat_list)

    assert [t.price_paid for t in tickets] == [None, None, None]
