"""Bulk hold/kill/release with per-seat rejection of ticketed seats."""

import pytest

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeatOverride, Ticket, TicketTier, VenueSeat
from events.service.seating import overrides as overrides_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="override_ticketholder@example.com", email="override_ticketholder@example.com")


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="General", price=10.00, payment_method=TicketTier.PaymentMethod.ONLINE
    )


def test_apply_and_release(seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    resp = overrides_service.apply_overrides(
        event,
        set_items=[(seats[0].id, "held", "house"), (seats[1].id, "killed", "camera")],
        release_seat_ids=[],
    )
    assert resp.applied == 2
    assert resp.rejected == {}
    assert EventSeatOverride.objects.filter(event=event).count() == 2

    resp = overrides_service.apply_overrides(event, set_items=[], release_seat_ids=[seats[0].id])
    assert resp.released == 1
    assert EventSeatOverride.objects.filter(event=event).count() == 1


def test_killing_ticketed_seat_rejected_per_seat(
    seated_event: tuple[Event, list[VenueSeat]], other_user: RevelUser, ticket_tier: TicketTier
) -> None:
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        guest_name="Someone",
    )
    resp = overrides_service.apply_overrides(
        event,
        set_items=[(seats[0].id, "killed", ""), (seats[1].id, "killed", "")],
        release_seat_ids=[],
    )
    assert resp.applied == 1
    assert str(seats[0].id) in {str(k) for k in resp.rejected}
    assert resp.rejected[seats[0].id] == "ticketed"
    assert not EventSeatOverride.objects.filter(event=event, seat=seats[0]).exists()
    assert EventSeatOverride.objects.filter(event=event, seat=seats[1]).exists()


def test_pending_ticket_also_rejected(
    seated_event: tuple[Event, list[VenueSeat]], other_user: RevelUser, ticket_tier: TicketTier
) -> None:
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        status=Ticket.TicketStatus.PENDING,
        guest_name="Someone",
    )
    resp = overrides_service.apply_overrides(event, set_items=[(seats[0].id, "held", "")], release_seat_ids=[])
    assert resp.applied == 0
    assert resp.rejected[seats[0].id] == "ticketed"


def test_cancelled_ticket_does_not_block(
    seated_event: tuple[Event, list[VenueSeat]], other_user: RevelUser, ticket_tier: TicketTier
) -> None:
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        status=Ticket.TicketStatus.CANCELLED,
        guest_name="Someone",
    )
    resp = overrides_service.apply_overrides(event, set_items=[(seats[0].id, "killed", "broken")], release_seat_ids=[])
    assert resp.applied == 1
    assert resp.rejected == {}


def test_upsert_updates_existing(seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    overrides_service.apply_overrides(event, set_items=[(seats[0].id, "held", "house")], release_seat_ids=[])
    overrides_service.apply_overrides(event, set_items=[(seats[0].id, "killed", "broken")], release_seat_ids=[])
    ov = EventSeatOverride.objects.get(event=event, seat=seats[0])
    assert ov.status == EventSeatOverride.OverrideStatus.KILLED
    assert ov.reason == "broken"
    assert EventSeatOverride.objects.filter(event=event, seat=seats[0]).count() == 1


def test_unknown_seat_rejected(seated_event: tuple[Event, list[VenueSeat]]) -> None:
    import uuid

    event, _ = seated_event
    ghost = uuid.uuid4()
    resp = overrides_service.apply_overrides(event, set_items=[(ghost, "held", "")], release_seat_ids=[])
    assert resp.applied == 0
    assert resp.rejected[ghost] == "unknown_seat"
