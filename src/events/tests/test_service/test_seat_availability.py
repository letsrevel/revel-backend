"""Availability payload: sparse statuses, standing counts, my_holds."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeatOverride, SeatHold, Ticket, TicketTier, VenueSeat, VenueSector
from events.service.seating import availability

pytestmark = pytest.mark.django_db


@pytest.fixture
def revel_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="seat_viewer@example.com", email="seat_viewer@example.com")


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="other_viewer@example.com", email="other_viewer@example.com")


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="General", price=10.00, payment_method=TicketTier.PaymentMethod.ONLINE
    )


def test_sparse_statuses(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    ticket_tier: TicketTier,
) -> None:
    event, seats = seated_event
    now = timezone.now()
    Ticket.objects.create(
        event=event, tier=ticket_tier, user=other_user, seat=seats[0], sector=seats[0].sector, guest_name="Someone"
    )
    SeatHold.objects.create(
        event=event, seat=seats[1], user=other_user, acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    EventSeatOverride.objects.create(
        event=event, seat=seats[2], status=EventSeatOverride.OverrideStatus.KILLED, reason="broken"
    )
    seats[3].is_active = False
    seats[3].save(update_fields=["is_active"])
    payload = availability.build_availability(event, user=revel_user, guest_session=None)
    assert payload.seats[seats[0].id] == "sold"
    assert payload.seats[seats[1].id] == "held"
    assert payload.seats[seats[2].id] == "blocked"
    assert payload.seats[seats[3].id] == "blocked"  # decommissioned reported blocked
    assert seats[4].id not in payload.seats  # absent = available


def test_checked_in_seat_reported_sold(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    ticket_tier: TicketTier,
) -> None:
    """A CHECKED_IN ticket occupies its seat exactly like an ACTIVE one — the
    unique_ticket_event_seat constraint covers all non-cancelled statuses."""
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        guest_name="Someone",
        status=Ticket.TicketStatus.CHECKED_IN,
        checked_in_at=timezone.now(),
    )
    payload = availability.build_availability(event, user=revel_user, guest_session=None)
    assert payload.seats[seats[0].id] == "sold"


def test_expired_hold_not_reported(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seats[0], user=other_user, acquired_at=now, expires_at=now - timedelta(seconds=1)
    )
    payload = availability.build_availability(event, user=revel_user, guest_session=None)
    assert seats[0].id not in payload.seats


def test_own_holds_listed(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seats[0], user=revel_user, acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    payload = availability.build_availability(event, user=revel_user, guest_session=None)
    assert payload.my_holds == [seats[0].id]
    assert payload.seats[seats[0].id] == "held"
    assert payload.my_holds_expire_at is not None


def test_guest_own_holds_listed(seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seats[0], guest_session="guest-abc", acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    payload = availability.build_availability(event, user=None, guest_session="guest-abc")
    assert payload.my_holds == [seats[0].id]


def test_sold_wins_over_held_on_same_seat(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    ticket_tier: TicketTier,
) -> None:
    event, seats = seated_event
    now = timezone.now()
    # Create a sold ticket on seats[0]
    Ticket.objects.create(
        event=event, tier=ticket_tier, user=other_user, seat=seats[0], sector=seats[0].sector, guest_name="Someone"
    )
    # Create a held seat on the same seat
    SeatHold.objects.create(
        event=event, seat=seats[0], user=revel_user, acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    # Verify sold takes precedence over held
    payload = availability.build_availability(event, user=revel_user, guest_session=None)
    assert payload.seats[seats[0].id] == "sold"


def test_standing_counts(
    seated_event: tuple[Event, list[VenueSeat]], other_user: RevelUser, ticket_tier: TicketTier
) -> None:
    event, _ = seated_event
    assert event.venue is not None
    standing = VenueSector.objects.create(venue=event.venue, name="Pit", kind=VenueSector.Kind.STANDING, capacity=300)
    ticket_tier.sector = standing
    ticket_tier.save()
    Ticket.objects.create(event=event, tier=ticket_tier, user=other_user, sector=standing, guest_name="Someone")
    payload = availability.build_availability(event, user=None, guest_session=None)
    assert payload.standing[standing.id].capacity == 300
    assert payload.standing[standing.id].taken == 1
