"""Box-office endpoints: POST /event-admin/{event_id}/seating/sell and /seating/reseat."""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, OrganizationStaff, Ticket, TicketTier, VenueSeat

pytestmark = pytest.mark.django_db


def _sell_url(event: Event) -> str:
    return reverse("api:event_seating_sell", kwargs={"event_id": event.pk})


def _reseat_url(event: Event) -> str:
    return reverse("api:event_seating_reseat", kwargs={"event_id": event.pk})


@pytest.fixture
def tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="Stalls", price=Decimal("25.00"), payment_method=TicketTier.PaymentMethod.ONLINE
    )


def test_owner_can_sell_to_guest_email(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "at_the_door",
        "email": "walkup@example.com",
        "guest_name": "Walk Up",
    }
    resp = organization_owner_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == Ticket.TicketStatus.ACTIVE
    assert body["seat"]["id"] == str(seats[0].id)
    assert body["guest_name"] == "Walk Up"
    ticket = Ticket.objects.get(pk=body["id"])
    assert ticket.user.email == "walkup@example.com"
    assert ticket.user.guest is True


def test_owner_can_sell_to_existing_user(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
    public_user: RevelUser,
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[1].id),
        "tier_id": str(tier.id),
        "payment_method": "free",
        "user_id": str(public_user.id),
    }
    resp = organization_owner_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content
    ticket = Ticket.objects.get(pk=resp.json()["id"])
    assert ticket.user == public_user
    assert ticket.price_paid == Decimal("0.00")


def test_sell_requires_exactly_one_recipient(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
    public_user: RevelUser,
) -> None:
    event, seats = seated_event
    base = {"seat_id": str(seats[0].id), "tier_id": str(tier.id), "payment_method": "free"}
    both = base | {"email": "x@example.com", "user_id": str(public_user.id)}
    neither = base
    for payload in (both, neither):
        resp = organization_owner_client.post(
            _sell_url(event), data=orjson.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 422, resp.content


def test_sell_rejects_online_payment_method(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "online",
        "email": "walkup@example.com",
    }
    resp = organization_owner_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 422, resp.content


def test_sell_tier_must_belong_to_event(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    organization: object,
) -> None:
    event, seats = seated_event
    other_event = Event.objects.create(
        organization=event.organization, name="Other", slug="other", start=event.start, status="open"
    )
    foreign_tier = TicketTier.objects.create(event=other_event, name="Foreign", price=Decimal("10.00"))
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(foreign_tier.id),
        "payment_method": "free",
        "email": "walkup@example.com",
    }
    resp = organization_owner_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 404


def test_staff_with_manage_tickets_can_sell(
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
) -> None:
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = True
    staff_member.permissions = perms
    staff_member.save()

    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "at_the_door",
        "email": "walkup@example.com",
    }
    resp = organization_staff_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 200, resp.content


def test_staff_without_manage_tickets_cannot_sell(
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
    seated_event: tuple[Event, list[VenueSeat]],
    tier: TicketTier,
) -> None:
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "free",
        "email": "walkup@example.com",
    }
    resp = organization_staff_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 403


def test_nonmember_cannot_sell(
    nonmember_client: Client, seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "free",
        "email": "walkup@example.com",
    }
    resp = nonmember_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 403


def test_sell_killed_seat_returns_400(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier
) -> None:
    event, seats = seated_event
    EventSeatOverride.objects.create(event=event, seat=seats[0], status=EventSeatOverride.OverrideStatus.KILLED)
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(tier.id),
        "payment_method": "at_the_door",
        "email": "walkup@example.com",
    }
    resp = organization_owner_client.post(_sell_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert resp.status_code == 400


# ---- reseat ----


@pytest.fixture
def seated_ticket(seated_event: tuple[Event, list[VenueSeat]], tier: TicketTier, public_user: RevelUser) -> Ticket:
    event, seats = seated_event
    return Ticket.objects.create(
        event=event, tier=tier, user=public_user, seat=seats[0], sector=seats[0].sector, guest_name="Buyer"
    )


def test_owner_can_reseat(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket
) -> None:
    event, seats = seated_event
    payload = {"ticket_id": str(seated_ticket.id), "target_seat_id": str(seats[3].id)}
    resp = organization_owner_client.post(
        _reseat_url(event), data=orjson.dumps(payload), content_type="application/json"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["seat"]["id"] == str(seats[3].id)
    seated_ticket.refresh_from_db()
    assert seated_ticket.seat_id == seats[3].id


def test_staff_without_manage_tickets_cannot_reseat(
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
    seated_event: tuple[Event, list[VenueSeat]],
    seated_ticket: Ticket,
) -> None:
    perms = staff_member.permissions
    perms["default"]["manage_tickets"] = False
    staff_member.permissions = perms
    staff_member.save()

    event, seats = seated_event
    payload = {"ticket_id": str(seated_ticket.id), "target_seat_id": str(seats[3].id)}
    resp = organization_staff_client.post(
        _reseat_url(event), data=orjson.dumps(payload), content_type="application/json"
    )
    assert resp.status_code == 403


def test_reseat_checked_in_ticket_returns_400(
    organization_owner_client: Client, seated_event: tuple[Event, list[VenueSeat]], seated_ticket: Ticket
) -> None:
    event, seats = seated_event
    seated_ticket.status = Ticket.TicketStatus.CHECKED_IN
    seated_ticket.save(update_fields=["status"])
    payload = {"ticket_id": str(seated_ticket.id), "target_seat_id": str(seats[3].id)}
    resp = organization_owner_client.post(
        _reseat_url(event), data=orjson.dumps(payload), content_type="application/json"
    )
    assert resp.status_code == 400
