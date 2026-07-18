"""Admin seating overrides controller: PUT /event-admin/{event_id}/seating/overrides."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, Ticket, TicketTier, VenueSeat

pytestmark = pytest.mark.django_db


def _url(event: Event) -> str:
    return reverse("api:event_seating_overrides", kwargs={"event_id": event.pk})


def test_owner_can_apply_overrides(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    payload = {
        "set": [
            {"seat_id": str(seats[0].id), "status": "held", "reason": "house"},
            {"seat_id": str(seats[1].id), "status": "killed", "reason": "camera"},
        ],
        "release_seat_ids": [],
    }
    resp = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["applied"] == 2
    assert EventSeatOverride.objects.filter(event=event).count() == 2


def test_owner_can_release_overrides(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    EventSeatOverride.objects.create(event=event, seat=seats[0], status="held", reason="house")
    payload = {"set": [], "release_seat_ids": [str(seats[0].id)]}
    resp = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["released"] == 1
    assert not EventSeatOverride.objects.filter(event=event).exists()


def test_nonmember_gets_403(nonmember_client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    payload = {"set": [{"seat_id": str(seats[0].id), "status": "held", "reason": ""}], "release_seat_ids": []}
    resp = nonmember_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 403


def test_ticketed_seat_lands_in_rejected_and_serializes(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], public_user: RevelUser
) -> None:
    """A rejected seat (UUID dict key) must serialize to valid JSON without error."""
    event, seats = seated_event
    tier = TicketTier.objects.create(
        event=event, name="General", price=10.00, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    Ticket.objects.create(
        event=event, tier=tier, user=public_user, seat=seats[0], sector=seats[0].sector, guest_name="Someone"
    )
    payload = {
        "set": [
            {"seat_id": str(seats[0].id), "status": "killed", "reason": ""},
            {"seat_id": str(seats[1].id), "status": "killed", "reason": ""},
        ],
        "release_seat_ids": [],
    }
    resp = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["applied"] == 1
    assert body["rejected"][str(seats[0].id)] == "ticketed"
