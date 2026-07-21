"""Public seating controller: chart, availability, holds (POST/DELETE)."""

import pytest
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, PriceCategory, TicketTier, VenueSeat
from events.service.guest_hold_session import GUEST_HOLD_COOKIE
from events.service.seating import holds as holds_service

pytestmark = pytest.mark.django_db


def _seated_tier(event: Event, seats: list[VenueSeat], *, paint: bool = True) -> TicketTier:
    """Create a best-available tier; paint the seats with its category unless paint=False."""
    venue = event.venue
    assert venue is not None
    cat = PriceCategory.objects.create(venue=venue, name="Orchestra", color="#00aa00")
    if paint:
        for s in seats:
            s.default_price_category = cat
            s.save(update_fields=["default_price_category"])
    return TicketTier.objects.create(
        event=event,
        name="Std",
        sector=seats[0].sector,
        category_prices={str(cat.id): "0"},
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )


def _zone(tier: TicketTier) -> str:
    """The single zone of a ``_seated_tier``: v3 makes the buyer name it per request."""
    return str(next(iter(tier.category_prices)))


def test_chart_returns_sectors_and_seats(client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    resp = client.get(f"/api/events/{event.id}/seating/chart")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["venue_name"] == "Hall"
    assert len(body["sectors"]) == 1
    assert len(body["sectors"][0]["seats"]) == len(seats)


def test_chart_serializes_legacy_pair_shape(client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    """Sector shapes stored in the legacy ``[[x, y], ...]`` format must serialize as ``{x, y}`` dicts."""
    event, seats = seated_event
    sector = seats[0].sector
    sector.shape = [[0, 0], [4, 0], [4, 2], [0, 2]]
    sector.save(update_fields=["shape"])
    resp = client.get(f"/api/events/{event.id}/seating/chart")
    assert resp.status_code == 200, resp.content
    assert resp.json()["sectors"][0]["shape"] == [
        {"x": 0.0, "y": 0.0},
        {"x": 4.0, "y": 0.0},
        {"x": 4.0, "y": 2.0},
        {"x": 0.0, "y": 2.0},
    ]


def test_chart_404_when_event_has_no_venue(client: Client, event: Event) -> None:
    resp = client.get(f"/api/events/{event.id}/seating/chart")
    assert resp.status_code == 404


def test_availability_reports_sparse_statuses(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]], public_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=public_user, guest_session=None)
    resp = member_client.get(f"/api/events/{event.id}/seating/availability")
    assert resp.status_code == 200, resp.content
    assert resp.json()["seats"][str(seats[0].id)] == "held"


def test_anonymous_hold_sets_guest_cookie_and_holds(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    resp = client.post(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(seats[0].id)]},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert GUEST_HOLD_COOKIE in resp.cookies
    assert resp.cookies[GUEST_HOLD_COOKIE]["httponly"]
    assert resp.json()["held_seat_ids"] == [str(seats[0].id)]


def test_authenticated_hold(member_client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(seats[1].id)]},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["held_seat_ids"] == [str(seats[1].id)]
    assert resp.json()["conflict_reason"] is None
    assert GUEST_HOLD_COOKIE not in resp.cookies  # no guest cookie for authenticated user


def test_conflicting_hold_returns_409(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]], public_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=public_user, guest_session=None)
    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(seats[0].id)]},
        content_type="application/json",
    )
    assert resp.status_code == 409, resp.content
    assert resp.json()["conflicts"] == [str(seats[0].id)]
    assert resp.json()["conflict_reason"] == "unavailable"


def test_over_cap_hold_returns_capacity_reason(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    event.max_tickets_per_user = 2
    event.save(update_fields=["max_tickets_per_user"])
    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(s.id) for s in seats[:3]]},
        content_type="application/json",
    )
    assert resp.status_code == 409, resp.content
    assert resp.json()["conflict_reason"] == "capacity"


def test_release_seats(
    member_client: Client, member_user: RevelUser, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=member_user, guest_session=None)
    resp = member_client.delete(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(seats[0].id)]},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    avail = member_client.get(f"/api/events/{event.id}/seating/availability")
    assert str(seats[0].id) not in avail.json()["seats"]


def test_best_available_hold_returns_adjacent_seats(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    tier = _seated_tier(event, seats)
    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds/best-available",
        data={"tier_id": str(tier.id), "quantity": 2, "price_category_id": _zone(tier)},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    held = resp.json()["held_seat_ids"]
    assert len(held) == 2
    by_index = {str(s.id): s.adjacency_index for s in seats}
    indices = sorted(by_index[h] for h in held)
    assert indices[1] - indices[0] == 1  # adjacent


def test_best_available_hold_409_when_no_block_fits(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    tier = _seated_tier(event, seats, paint=False)  # category has no seats
    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds/best-available",
        data={"tier_id": str(tier.id), "quantity": 2, "price_category_id": _zone(tier)},
        content_type="application/json",
    )
    assert resp.status_code == 409, resp.content
    # Same HoldResponseSchema shape as every other hold 409, not an HttpError {detail}.
    body = resp.json()
    assert body["conflict_reason"] == "no_block"
    assert body["held_seat_ids"] == []
    assert body["conflicts"] == []
    assert body["expires_at"] is None


def test_anonymous_best_available_hold_sets_guest_cookie(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    tier = _seated_tier(event, seats)
    resp = client.post(
        f"/api/events/{event.id}/seating/holds/best-available",
        data={"tier_id": str(tier.id), "quantity": 2, "price_category_id": _zone(tier)},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert GUEST_HOLD_COOKIE in resp.cookies
    assert resp.cookies[GUEST_HOLD_COOKIE]["httponly"]


def test_best_available_hold_400_without_a_zone(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    """A tier that prices zones cannot guess which one the buyer meant (#749)."""
    event, seats = seated_event
    tier = _seated_tier(event, seats)

    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds/best-available",
        data={"tier_id": str(tier.id), "quantity": 2},
        content_type="application/json",
    )

    assert resp.status_code == 400, resp.content
    assert "Orchestra" in resp.json()["detail"]  # the ZONE name, distinct from the tier name "Std"


def test_best_available_hold_400_for_a_zone_the_tier_does_not_price(
    member_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    tier = _seated_tier(event, seats)
    venue = event.venue
    assert venue is not None
    stranger = PriceCategory.objects.create(venue=venue, name="Boxes", color="#0000aa")

    resp = member_client.post(
        f"/api/events/{event.id}/seating/holds/best-available",
        data={"tier_id": str(tier.id), "quantity": 2, "price_category_id": str(stranger.id)},
        content_type="application/json",
    )

    assert resp.status_code == 400, resp.content
    assert "Orchestra" in resp.json()["detail"]
