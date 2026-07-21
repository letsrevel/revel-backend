"""Public seating controller: chart, availability, holds (POST/DELETE)."""

import typing as t

import pytest
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, PriceCategory, TicketTier, VenueSeat
from events.service.guest_hold_session import GUEST_HOLD_COOKIE
from events.service.seating import holds as holds_service

pytestmark = pytest.mark.django_db

# Whole anonymous chart request: event visibility lookups, the venue row, then build_chart's
# own prefetches (sectors, seats, price categories). Measured, and unchanged by #755 —
# ``metadata`` rides on the venue row that is already fetched.
_CHART_QUERIES = 9


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


def test_chart_projects_metadata_to_whitelisted_keys(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    """The anonymous chart serves a whitelisted projection, not the verbatim blob (#761).

    Venue level keeps only ``stage``; sector level only ``transform``/``aisles`` —
    exactly the keys the shipped buyer renderer reads. Whitelisted values pass
    through verbatim; everything else the designer wrote is stripped.
    """
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    stage = {"shape": [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}], "label": "Stage"}
    venue.metadata = {
        "stage": stage,
        "floors": [{"id": "ground", "name": "Ground", "order": 0}],
        "designer_note": "loading dock code 4711",
    }
    venue.save(update_fields=["metadata"])
    sector = seats[0].sector
    transform = {"x": 0.0, "y": 120.0, "rotation": 90.0}
    aisles = {"verticalAisles": [4], "horizontalAisles": [2], "invertRowOrder": False}
    sector.metadata = {"transform": transform, "aisles": aisles, "floor": "ground"}
    sector.save(update_fields=["metadata"])

    resp = client.get(f"/api/events/{event.id}/seating/chart")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["metadata"] == {"stage": stage}
    assert body["sectors"][0]["metadata"] == {"transform": transform, "aisles": aisles}


def test_chart_projects_non_whitelisted_metadata_to_empty_object(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    """An object with no whitelisted keys projects to ``{}`` — not ``null`` (#761)."""
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    venue.metadata = {"scratch": "not for buyers"}
    venue.save(update_fields=["metadata"])
    sector = seats[0].sector
    sector.metadata = {"floor": "ground"}
    sector.save(update_fields=["metadata"])

    resp = client.get(f"/api/events/{event.id}/seating/chart")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["metadata"] == {}
    assert body["sectors"][0]["metadata"] == {}


def test_chart_metadata_is_null_when_unset(client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    """No designer data serialises as ``null`` — never ``{}`` — so the FE has one emptiness check."""
    event, _seats = seated_event
    resp = client.get(f"/api/events/{event.id}/seating/chart")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "metadata" in body
    assert body["metadata"] is None
    # Same rule at sector level: the projection must not turn null into {}.
    assert body["sectors"][0]["metadata"] is None


def test_chart_query_count_is_unaffected_by_venue_metadata(
    client: Client, seated_event: tuple[Event, list[VenueSeat]], django_assert_num_queries: t.Any
) -> None:
    """``metadata`` rides on the venue row already fetched — the chart budget must not move."""
    event, _seats = seated_event
    venue = event.venue
    assert venue is not None
    client.get(f"/api/events/{event.id}/seating/chart")  # warm any per-process caches
    with django_assert_num_queries(_CHART_QUERIES):
        assert client.get(f"/api/events/{event.id}/seating/chart").status_code == 200

    venue.metadata = {"stage": {"label": "Stage"}}
    venue.save(update_fields=["metadata"])
    with django_assert_num_queries(_CHART_QUERIES):
        assert client.get(f"/api/events/{event.id}/seating/chart").status_code == 200


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
