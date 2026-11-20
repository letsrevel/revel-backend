"""Staff-facing responses mask an account that only staff linked to the organization."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    OrganizationMember,
    Ticket,
    TicketSaleSource,
    TicketTier,
    VenueSeat,
)

pytestmark = pytest.mark.django_db

MASKED_FIELDS = ("phone_number", "first_name", "last_name", "preferred_name", "pronouns", "bio")


@pytest.fixture
def profiled(public_user: RevelUser) -> RevelUser:
    """An account with a full profile, so any leak is visible."""
    public_user.first_name, public_user.last_name = "Legal", "Person"
    public_user.preferred_name = "Pref"
    public_user.pronouns, public_user.bio = "they/them", "about me"
    public_user.phone_number = "+4366012345678"
    public_user.email_verified = True
    public_user.save()
    return public_user


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


def _ticket(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    source: str | None,
    guest_name: str = "Typed Name",
    **extra: t.Any,
) -> Ticket:
    ticket = Ticket.objects.create(
        event=event, tier=tier, user=user, guest_name=guest_name, status=Ticket.TicketStatus.ACTIVE, **extra
    )
    Ticket.objects.filter(pk=ticket.pk).update(sale_source=source)  # editable=False
    ticket.refresh_from_db()
    return ticket


def _assert_masked(user: dict[str, t.Any], account: RevelUser, display_name: str) -> None:
    assert user["id"] == str(account.id)
    assert user["known"] is False
    assert user["email"] == account.email
    assert user["display_name"] == display_name
    for field in MASKED_FIELDS:
        assert user[field] is None, field
    assert user["profile_picture_url"] is None


def _assert_unmasked(user: dict[str, t.Any], account: RevelUser, *, phone: bool) -> None:
    assert user["known"] is True
    assert user["first_name"] == "Legal"
    assert user["last_name"] == "Person"
    assert user["pronouns"] == "they/them"
    assert user["bio"] == "about me"
    assert user["phone_number"] == ("+4366012345678" if phone else None)


# ---- tickets: list, detail, check-in ----


@pytest.mark.parametrize("source", [TicketSaleSource.BOX_OFFICE_COMP, TicketSaleSource.BOX_OFFICE_SALE])
def test_ticket_list_and_detail_mask_box_office_holder(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser, source: str
) -> None:
    ticket = _ticket(event, event_ticket_tier, profiled, source)
    listing = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    assert listing.status_code == 200, listing.content
    _assert_masked(listing.json()["results"][0]["user"], profiled, "Typed Name")
    detail = organization_owner_client.get(
        reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    )
    assert detail.status_code == 200, detail.content
    _assert_masked(detail.json()["user"], profiled, "Typed Name")
    assert "Legal" not in listing.content.decode() + detail.content.decode()


def test_masked_display_name_falls_back_to_email(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser
) -> None:
    _ticket(event, event_ticket_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP, guest_name="")
    resp = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    assert resp.json()["results"][0]["user"]["display_name"] == profiled.email


@pytest.mark.parametrize("source", [TicketSaleSource.CHECKOUT, None])
def test_ticket_list_and_detail_unmask_self_bought_holder(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    profiled: RevelUser,
    source: str | None,
) -> None:
    ticket = _ticket(event, event_ticket_tier, profiled, source)
    listing = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    _assert_unmasked(listing.json()["results"][0]["user"], profiled, phone=True)
    detail = organization_owner_client.get(
        reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    )
    _assert_unmasked(detail.json()["user"], profiled, phone=True)


def test_member_is_never_masked(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    profiled: RevelUser,
    organization_membership: OrganizationMember,
) -> None:
    OrganizationMember.objects.create(organization=event.organization, user=profiled)
    _ticket(event, event_ticket_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP)
    resp = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    _assert_unmasked(resp.json()["results"][0]["user"], profiled, phone=True)


def test_other_ticket_from_checkout_unmasks_comp_holder(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser
) -> None:
    _ticket(event, event_ticket_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP)
    _ticket(event, event_ticket_tier, profiled, TicketSaleSource.CHECKOUT)
    resp = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    assert {r["user"]["known"] for r in resp.json()["results"]} == {True}


def test_ticket_search_does_not_match_masked_profile_names(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser
) -> None:
    _ticket(event, event_ticket_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP)
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    assert organization_owner_client.get(url, {"search": "Legal"}).json()["count"] == 0
    assert organization_owner_client.get(url, {"search": "Typed"}).json()["count"] == 1
    assert organization_owner_client.get(url, {"search": profiled.email}).json()["count"] == 1


def test_ticket_list_masking_adds_no_per_row_queries(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    django_user_model: type[RevelUser],
) -> None:
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    def add(n: int, offset: int) -> None:
        for i in range(n):
            account = django_user_model.objects.create_user(
                username=f"q{offset + i}", email=f"q{offset + i}@example.com", password="pass"
            )
            source = TicketSaleSource.BOX_OFFICE_COMP if i % 2 else TicketSaleSource.CHECKOUT
            _ticket(event, event_ticket_tier, account, source)

    def count() -> int:
        organization_owner_client.get(url)  # warm caches (content types, permissions)
        with CaptureQueriesContext(connection) as ctx:
            resp = organization_owner_client.get(url)
        assert resp.status_code == 200
        return len(ctx)

    add(1, 0)
    one = count()
    add(4, 1)
    assert count() == one


def test_check_in_masks_box_office_holder_without_phone(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser
) -> None:
    ticket = _ticket(event, event_ticket_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP)
    resp = organization_owner_client.post(
        reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": str(ticket.pk)}),
        data=orjson.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_masked(resp.json()["user"], profiled, "Typed Name")


def test_check_in_known_holder_has_no_phone(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, profiled: RevelUser
) -> None:
    ticket = _ticket(event, event_ticket_tier, profiled, TicketSaleSource.CHECKOUT)
    resp = organization_owner_client.post(
        reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": str(ticket.pk)}),
        data=orjson.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_unmasked(resp.json()["user"], profiled, phone=False)


@pytest.mark.parametrize(
    ("source", "unmasked"), [(TicketSaleSource.CHECKOUT, True), (TicketSaleSource.BOX_OFFICE_SALE, False)]
)
def test_check_in_of_a_pending_offline_ticket_unmasks_only_a_self_made_ticket(
    organization_owner_client: Client, event: Event, profiled: RevelUser, source: str, unmasked: bool
) -> None:
    """The check-in turns a pending ticket attending, which confers standing, so the response matches the next read."""
    offline_tier = TicketTier.objects.create(
        event=event, name="Door", price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.OFFLINE
    )
    ticket = _ticket(event, offline_tier, profiled, source)
    Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    resp = organization_owner_client.post(
        reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": str(ticket.pk)}),
        data=orjson.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    if unmasked:
        _assert_unmasked(resp.json()["user"], profiled, phone=False)
    else:
        _assert_masked(resp.json()["user"], profiled, "Typed Name")


def test_check_in_of_an_unconfirmed_guest_offline_ticket_stays_masked(
    organization_owner_client: Client, event: Event, profiled: RevelUser
) -> None:
    """A guest checkout never proved the email (#1430): checking it in confers no standing."""
    offline_tier = TicketTier.objects.create(
        event=event, name="Door", price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.OFFLINE
    )
    ticket = _ticket(event, offline_tier, profiled, TicketSaleSource.CHECKOUT, unconfirmed_guest=True)
    Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    resp = organization_owner_client.post(
        reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": str(ticket.pk)}),
        data=orjson.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_masked(resp.json()["user"], profiled, "Typed Name")


# ---- sell / reseat ----


@pytest.fixture
def seated_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="Stalls", price=Decimal("25.00"), payment_method=TicketTier.PaymentMethod.ONLINE
    )


def test_sell_by_email_masks_account_and_shows_typed_name(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    seated_tier: TicketTier,
    profiled: RevelUser,
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(seated_tier.id),
        "payment_method": "at_the_door",
        "email": profiled.email,
        "guest_name": "Door Guest",
    }
    resp = organization_owner_client.post(
        reverse("api:event_seating_sell", kwargs={"event_id": event.pk}),
        data=orjson.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_masked(resp.json()["user"], profiled, "Door Guest")


def test_sell_without_ticket_name_uses_the_typed_name_not_the_account_name(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    seated_tier: TicketTier,
    profiled: RevelUser,
) -> None:
    event, seats = seated_event
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(seated_tier.id),
        "payment_method": "free",
        "email": profiled.email,
        "first_name": "Typed",
        "last_name": "Person",
    }
    resp = organization_owner_client.post(
        reverse("api:event_seating_sell", kwargs={"event_id": event.pk}),
        data=orjson.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["guest_name"] == "Typed Person"
    _assert_masked(resp.json()["user"], profiled, "Typed Person")


def test_sell_to_known_member_is_unmasked(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    seated_tier: TicketTier,
    profiled: RevelUser,
) -> None:
    event, seats = seated_event
    OrganizationMember.objects.create(organization=event.organization, user=profiled)
    payload = {
        "seat_id": str(seats[0].id),
        "tier_id": str(seated_tier.id),
        "payment_method": "free",
        "user_id": str(profiled.id),
    }
    resp = organization_owner_client.post(
        reverse("api:event_seating_sell", kwargs={"event_id": event.pk}),
        data=orjson.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_unmasked(resp.json()["user"], profiled, phone=True)


def test_reseat_masks_box_office_holder_and_unmasks_self_bought(
    organization_owner_client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    seated_tier: TicketTier,
    profiled: RevelUser,
) -> None:
    event, seats = seated_event
    url = reverse("api:event_seating_reseat", kwargs={"event_id": event.pk})
    ticket = _ticket(
        event, seated_tier, profiled, TicketSaleSource.BOX_OFFICE_COMP, seat=seats[0], sector=seats[0].sector
    )
    resp = organization_owner_client.post(
        url,
        data=orjson.dumps({"ticket_id": str(ticket.id), "target_seat_id": str(seats[1].id)}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_masked(resp.json()["user"], profiled, "Typed Name")

    Ticket.objects.filter(pk=ticket.pk).update(sale_source=TicketSaleSource.CHECKOUT)
    resp = organization_owner_client.post(
        url,
        data=orjson.dumps({"ticket_id": str(ticket.id), "target_seat_id": str(seats[2].id)}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _assert_unmasked(resp.json()["user"], profiled, phone=True)


# ---- RSVPs ----


def test_rsvp_list_masks_staff_rsvp_and_unmasks_self_rsvp(
    organization_owner_client: Client,
    event: Event,
    profiled: RevelUser,
    django_user_model: type[RevelUser],
) -> None:
    other = django_user_model.objects.create_user(username="selfrsvp", email="self@example.com", password="pass")
    EventRSVP.objects.create(event=event, user=profiled, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.STAFF)
    EventRSVP.objects.create(event=event, user=other, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.SELF)
    resp = organization_owner_client.get(reverse("api:list_rsvps", kwargs={"event_id": event.pk}))
    assert resp.status_code == 200, resp.content
    by_id = {r["user"]["id"]: r["user"] for r in resp.json()["results"]}
    _assert_masked(by_id[str(profiled.id)], profiled, profiled.email)
    assert by_id[str(other.id)]["known"] is True
    assert "Legal" not in resp.content.decode()


@pytest.mark.parametrize("list_name", ["api:list_rsvps", "api:list_event_invitations"])
def test_rsvp_and_invitation_search_does_not_match_account_names(
    organization_owner_client: Client, event: Event, profiled: RevelUser, list_name: str
) -> None:
    EventRSVP.objects.create(event=event, user=profiled, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.STAFF)
    EventInvitation.objects.create(event=event, user=profiled)
    url = reverse(list_name, kwargs={"event_id": event.pk})
    assert organization_owner_client.get(url, {"search": profiled.email}).json()["count"] == 1
    assert organization_owner_client.get(url, {"search": "Legal"}).json()["count"] == 0


def test_rsvp_never_includes_phone_even_when_known(
    organization_owner_client: Client, event: Event, profiled: RevelUser
) -> None:
    rsvp = EventRSVP.objects.create(
        event=event, user=profiled, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.SELF
    )
    listing = organization_owner_client.get(reverse("api:list_rsvps", kwargs={"event_id": event.pk}))
    detail = organization_owner_client.get(reverse("api:get_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk}))
    for user in (listing.json()["results"][0]["user"], detail.json()["user"]):
        _assert_unmasked(user, profiled, phone=False)


def test_rsvp_detail_and_update_mask_staff_rsvp(
    organization_owner_client: Client, event: Event, profiled: RevelUser
) -> None:
    rsvp = EventRSVP.objects.create(
        event=event, user=profiled, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.STAFF
    )
    detail = organization_owner_client.get(reverse("api:get_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk}))
    _assert_masked(detail.json()["user"], profiled, profiled.email)
    updated = organization_owner_client.put(
        reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk}),
        data=orjson.dumps({"status": "no"}),
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    _assert_masked(updated.json()["user"], profiled, profiled.email)


# ---- invitations ----


def test_invitation_list_masks_account_invited_by_email_and_never_includes_phone(
    organization_owner_client: Client,
    event: Event,
    profiled: RevelUser,
    django_user_model: type[RevelUser],
) -> None:
    other = django_user_model.objects.create_user(username="known", email="known@example.com", password="pass")
    other.first_name, other.last_name, other.phone_number = "Legal", "Person", "+4366099999999"
    other.pronouns, other.bio = "they/them", "about me"
    other.save()
    OrganizationMember.objects.create(organization=event.organization, user=other)
    EventInvitation.objects.create(event=event, user=profiled)
    EventInvitation.objects.create(event=event, user=other)
    resp = organization_owner_client.get(reverse("api:list_event_invitations", kwargs={"event_id": event.pk}))
    assert resp.status_code == 200, resp.content
    by_id = {r["user"]["id"]: r["user"] for r in resp.json()["results"]}
    _assert_masked(by_id[str(profiled.id)], profiled, profiled.email)
    known = by_id[str(other.id)]
    assert known["known"] is True
    assert known["first_name"] == "Legal"
    assert known["phone_number"] is None


def _invite_without_message(client: Client, event: Event, *accounts: RevelUser) -> None:
    resp = client.post(
        reverse("api:create_direct_invitations", kwargs={"event_id": event.pk}),
        data=orjson.dumps({"emails": [a.email for a in accounts]}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content


def test_default_invitation_message_does_not_carry_unknown_account_names(
    organization_owner_client: Client, event: Event, profiled: RevelUser
) -> None:
    """The default greeting of an invitation to an unknown account uses the typed email, so the list leaks nothing."""
    _invite_without_message(organization_owner_client, event, profiled)
    url = reverse("api:list_event_invitations", kwargs={"event_id": event.pk})
    resp = organization_owner_client.get(url)
    assert resp.status_code == 200, resp.content
    (row,) = resp.json()["results"]
    for name in ("Legal", "Person", "Pref"):
        assert name not in (row["custom_message"] or "")
        assert organization_owner_client.get(url, {"search": name}).json()["count"] == 0
    assert profiled.email in row["custom_message"]


def test_default_invitation_message_greets_known_member_by_name(
    organization_owner_client: Client, event: Event, profiled: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=event.organization, user=profiled)
    _invite_without_message(organization_owner_client, event, profiled)
    resp = organization_owner_client.get(reverse("api:list_event_invitations", kwargs={"event_id": event.pk}))
    (row,) = resp.json()["results"]
    assert profiled.get_display_name() in row["custom_message"]
