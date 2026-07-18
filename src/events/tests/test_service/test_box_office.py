"""Box-office door sales, comps, and reseat (spec §2, Phase 4)."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeatOverride,
    PriceCategory,
    SeatHold,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.service.seating import box_office

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(event: Event) -> TicketTier:
    """A paid online tier — box office sells against any tier regardless of its payment method."""
    return TicketTier.objects.create(
        event=event, name="Stalls", price=Decimal("25.00"), payment_method=TicketTier.PaymentMethod.ONLINE
    )


@pytest.fixture
def recipient(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="doorbuyer@example.com", email="doorbuyer@example.com")


def _live_hold(event: Event, seat: VenueSeat, *, user: RevelUser | None = None, guest_session: str = "") -> SeatHold:
    now = timezone.now()
    return SeatHold.objects.create(
        event=event,
        seat=seat,
        user=user,
        guest_session=guest_session,
        acquired_at=now,
        expires_at=now + timedelta(minutes=10),
    )


# ---- resolve_recipient ----


def test_resolve_recipient_creates_guest_for_new_email() -> None:
    user = box_office.resolve_recipient("new-guest@example.com", None, first_name="Jane", last_name="Doe")
    assert user.guest is True
    assert user.email == "new-guest@example.com"


def test_resolve_recipient_reuses_existing_non_guest_account(recipient: RevelUser) -> None:
    user = box_office.resolve_recipient(recipient.email, None)
    assert user == recipient


def test_resolve_recipient_by_user_id(recipient: RevelUser) -> None:
    assert box_office.resolve_recipient(None, recipient.id) == recipient


# ---- sell ----


def test_sell_at_the_door_on_free_seat(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    ticket = box_office.sell(
        event,
        tier,
        seat_id=seats[0].id,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        recipient=recipient,
    )
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.user == recipient
    assert ticket.seat == seats[0]
    assert ticket.sector == seats[0].sector
    assert ticket.venue == event.venue
    assert ticket.price_paid is None  # tier price applies for reporting
    tier.refresh_from_db()
    assert tier.quantity_sold == 1


def test_sell_comp_records_zero_price_paid(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    ticket = box_office.sell(
        event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.FREE, recipient=recipient
    )
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.price_paid == Decimal("0.00")


def test_sell_guest_name_defaults_to_recipient_display_name(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    ticket = box_office.sell(
        event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.FREE, recipient=recipient
    )
    assert ticket.guest_name == recipient.get_display_name()


def test_sell_on_held_seat_releases_override(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    EventSeatOverride.objects.create(
        event=event, seat=seats[0], status=EventSeatOverride.OverrideStatus.HELD, reason="promoter"
    )
    ticket = box_office.sell(
        event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
    )
    assert ticket.seat == seats[0]
    assert not EventSeatOverride.objects.filter(event=event, seat=seats[0]).exists()


def test_sell_on_killed_seat_rejected(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    EventSeatOverride.objects.create(
        event=event, seat=seats[0], status=EventSeatOverride.OverrideStatus.KILLED, reason="broken"
    )
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 400
    assert EventSeatOverride.objects.filter(event=event, seat=seats[0]).exists()


def test_sell_foreign_held_seat_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
    recipient: RevelUser,
    revel_user_factory: RevelUserFactory,
) -> None:
    event, seats = seated_event
    other = revel_user_factory(username="cartuser@example.com", email="cartuser@example.com")
    _live_hold(event, seats[0], user=other)
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 409


def test_sell_consumes_recipients_own_hold(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    _live_hold(event, seats[0], user=recipient)
    ticket = box_office.sell(
        event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
    )
    assert ticket.seat == seats[0]
    assert not SeatHold.objects.filter(event=event, seat=seats[0]).exists()


def test_sell_occupied_seat_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
    recipient: RevelUser,
    revel_user_factory: RevelUserFactory,
) -> None:
    event, seats = seated_event
    other = revel_user_factory(username="sitting@example.com", email="sitting@example.com")
    Ticket.objects.create(
        event=event, tier=tier, user=other, seat=seats[0], sector=seats[0].sector, guest_name="Someone"
    )
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 409


def test_sell_cancelled_ticket_does_not_block(
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
    recipient: RevelUser,
    revel_user_factory: RevelUserFactory,
) -> None:
    event, seats = seated_event
    other = revel_user_factory(username="wasthere@example.com", email="wasthere@example.com")
    Ticket.objects.create(
        event=event,
        tier=tier,
        user=other,
        seat=seats[0],
        sector=seats[0].sector,
        guest_name="Someone",
        status=Ticket.TicketStatus.CANCELLED,
    )
    ticket = box_office.sell(
        event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
    )
    assert ticket.seat == seats[0]


def test_sell_respects_tier_capacity(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    tier.total_quantity = 1
    tier.quantity_sold = 1
    tier.save(update_fields=["total_quantity", "quantity_sold"])
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 429


def test_sell_seat_from_another_venue_rejected(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, _seats = seated_event
    other_venue = Venue.objects.create(organization=event.organization, name="Other Hall")
    other_sector = VenueSector.objects.create(venue=other_venue, name="Balcony")
    foreign_seat = VenueSeat.objects.create(sector=other_sector, label="Z1")
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event,
            tier,
            seat_id=foreign_seat.id,
            payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
            recipient=recipient,
        )
    assert exc.value.status_code == 400


def test_sell_inactive_seat_rejected(
    seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser
) -> None:
    event, seats = seated_event
    seats[0].is_active = False
    seats[0].save(update_fields=["is_active"])
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=seats[0].id, payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 400


def test_sell_event_without_venue_rejected(event: Event, tier: TicketTier, recipient: RevelUser) -> None:
    with pytest.raises(HttpError) as exc:
        box_office.sell(
            event, tier, seat_id=uuid.uuid4(), payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, recipient=recipient
        )
    assert exc.value.status_code == 400


# ---- reseat ----


@pytest.fixture
def seated_ticket(seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, recipient: RevelUser) -> Ticket:
    event, seats = seated_event
    return Ticket.objects.create(
        event=event, tier=tier, user=recipient, seat=seats[0], sector=seats[0].sector, guest_name="Door Buyer"
    )


def test_reseat_happy_path(seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket) -> None:
    event, seats = seated_event
    old_venue_id = seated_ticket.venue_id
    ticket = box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert ticket.seat == seats[3]
    assert ticket.sector == seats[3].sector
    assert ticket.venue_id == old_venue_id
    seated_ticket.refresh_from_db()
    assert seated_ticket.seat == seats[3]
    # old seat is free again
    assert not Ticket.objects.filter(event=event, seat=seats[0]).exclude(status=Ticket.TicketStatus.CANCELLED).exists()


def test_reseat_across_sectors_updates_sector(
    seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket
) -> None:
    event, _seats = seated_event
    assert event.venue is not None
    other_sector = VenueSector.objects.create(venue=event.venue, name="Balcony")
    target = VenueSeat.objects.create(sector=other_sector, label="B1")
    ticket = box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=target.id)
    assert ticket.sector == other_sector


def test_reseat_cross_category_rejected(seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket) -> None:
    event, seats = seated_event
    assert event.venue is not None
    gold = PriceCategory.objects.create(venue=event.venue, name="Gold", color="#ffd700")
    seats[3].default_price_category = gold
    seats[3].save(update_fields=["default_price_category"])
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 400


def test_reseat_occupied_target_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    seated_ticket: Ticket,
    tier: TicketTier,
    revel_user_factory: RevelUserFactory,
) -> None:
    event, seats = seated_event
    other = revel_user_factory(username="neighbor@example.com", email="neighbor@example.com")
    Ticket.objects.create(
        event=event, tier=tier, user=other, seat=seats[3], sector=seats[3].sector, guest_name="Neighbor"
    )
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status", [EventSeatOverride.OverrideStatus.KILLED, EventSeatOverride.OverrideStatus.HELD])
def test_reseat_overridden_target_rejected(
    seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket, status: EventSeatOverride.OverrideStatus
) -> None:
    event, seats = seated_event
    EventSeatOverride.objects.create(event=event, seat=seats[3], status=status)
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 400


def test_reseat_foreign_held_target_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    seated_ticket: Ticket,
    revel_user_factory: RevelUserFactory,
) -> None:
    event, seats = seated_event
    other = revel_user_factory(username="holder@example.com", email="holder@example.com")
    _live_hold(event, seats[3], user=other)
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status", [Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED])
def test_reseat_non_reseatable_status_rejected(
    seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket, status: Ticket.TicketStatus
) -> None:
    event, seats = seated_event
    seated_ticket.status = status
    seated_ticket.save(update_fields=["status"])
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 400


def test_reseat_pending_ticket_allowed(seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket) -> None:
    event, seats = seated_event
    seated_ticket.status = Ticket.TicketStatus.PENDING
    seated_ticket.save(update_fields=["status"])
    ticket = box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert ticket.seat == seats[3]


def test_reseat_ticket_without_seat_rejected(event: Event, tier: TicketTier, recipient: RevelUser) -> None:
    ticket = Ticket.objects.create(event=event, tier=tier, user=recipient, guest_name="GA Buyer")
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=ticket.id, target_seat_id=uuid.uuid4())
    assert exc.value.status_code == 400


def test_reseat_to_same_seat_rejected(seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket) -> None:
    event, seats = seated_event
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[0].id)
    assert exc.value.status_code == 400


def test_reseat_inactive_target_rejected(seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket) -> None:
    event, seats = seated_event
    seats[3].is_active = False
    seats[3].save(update_fields=["is_active"])
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=seats[3].id)
    assert exc.value.status_code == 400


def test_reseat_target_in_other_venue_rejected(
    seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket
) -> None:
    event, _seats = seated_event
    other_venue = Venue.objects.create(organization=event.organization, name="Other Hall")
    other_sector = VenueSector.objects.create(venue=other_venue, name="Balcony")
    foreign_seat = VenueSeat.objects.create(sector=other_sector, label="Z1")
    with pytest.raises(HttpError) as exc:
        box_office.reseat(event, ticket_id=seated_ticket.id, target_seat_id=foreign_seat.id)
    assert exc.value.status_code == 400
