"""The ``X-Guest-Hold`` header carries the guest seat-hold session for cross-site (tenant host) clients (#1179).

A browser on a white-label custom domain never stores or sends the ``SameSite=Lax`` cookie set on
the API host, so the signed hold token is also returned in the hold response body and accepted
back in a request header. The cookie path keeps working.
"""

import typing as t
from decimal import Decimal
from unittest.mock import Mock, patch

import jwt
import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, SeatHold, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.service import guest as guest_service
from events.service.guest_hold_session import (
    GUEST_HOLD_COOKIE,
    GUEST_HOLD_HEADER,
    issue_guest_hold_token,
    resolve_request_guest_hold,
)

pytestmark = pytest.mark.django_db

HEADER_KEY = "HTTP_X_GUEST_HOLD"


@pytest.fixture
def seated_tier(organization: Organization, guest_event_with_tickets: Event) -> tuple[TicketTier, list[VenueSeat]]:
    """A free USER_CHOICE tier on a seated guest event, with five seats."""
    venue = Venue.objects.create(organization=organization, name="Hall", capacity=100)
    sector = VenueSector.objects.create(venue=venue, name="Main", capacity=50)
    seats = [
        VenueSeat.objects.create(sector=sector, label=f"A{i + 1}", row_label="A", number=str(i + 1), adjacency_index=i)
        for i in range(5)
    ]
    guest_event_with_tickets.venue = venue
    guest_event_with_tickets.save(update_fields=["venue"])
    tier = TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Reserved",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        price_type=TicketTier.PriceType.FIXED,
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        max_tickets_per_user=10,
    )
    return tier, seats


def _hold(client: Client, event: Event, seat: VenueSeat, **extra: t.Any) -> t.Any:
    return client.post(
        f"/api/events/{event.id}/seating/holds",
        data={"seat_ids": [str(seat.id)]},
        content_type="application/json",
        **extra,
    )


def _availability(client: Client, event: Event, **extra: t.Any) -> t.Any:
    return client.get(f"/api/events/{event.id}/seating/availability", **extra)


def _emailed_token_session(mock_send_email: Mock) -> str | None:
    """The hold-owner session embedded in the confirmation token passed to the email task."""
    raw = jwt.decode(
        mock_send_email.call_args[0][1],
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        options={"verify_aud": False},
    )
    session = raw.get("guest_session")
    return session if isinstance(session, str) else None


def _checkout(client: Client, event: Event, tier: TicketTier, seat: VenueSeat, **extra: t.Any) -> t.Any:
    return client.post(
        reverse("api:guest_ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk}),
        data={
            "email": "tenantbuyer@example.com",
            "first_name": "Tenant",
            "last_name": "Buyer",
            "tickets": [{"guest_name": "Tenant Buyer", "seat_id": str(seat.id)}],
        },
        content_type="application/json",
        **extra,
    )


class TestBodyToken:
    def test_anonymous_hold_returns_a_token_that_resolves(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        _, seats = seated_tier
        resp = _hold(Client(), guest_event_with_tickets, seats[0])

        assert resp.status_code == 200, resp.content
        token = resp.json()["guest_hold_token"]
        assert token
        assert token == resp.cookies[GUEST_HOLD_COOKIE].value  # cookie path unchanged
        hold = SeatHold.objects.get(event=guest_event_with_tickets, seat=seats[0])
        assert hold.guest_session
        # The token identifies the same session that owns the hold.
        request = Client().get("/api/", headers={GUEST_HOLD_HEADER: token}).wsgi_request
        resolved = resolve_request_guest_hold(request)
        assert resolved is not None and resolved.session_id == hold.guest_session

    def test_token_is_echoed_when_the_caller_already_has_a_session(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        _, seats = seated_tier
        token = _hold(Client(), guest_event_with_tickets, seats[0]).json()["guest_hold_token"]

        resp = _hold(Client(), guest_event_with_tickets, seats[1], **{HEADER_KEY: token})

        assert resp.json()["guest_hold_token"] == token
        assert GUEST_HOLD_COOKIE not in resp.cookies  # nothing minted
        assert SeatHold.objects.filter(event=guest_event_with_tickets).values("guest_session").distinct().count() == 1

    def test_409_conflict_still_returns_the_token_to_anonymous_callers(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        _, seats = seated_tier
        _hold(Client(), guest_event_with_tickets, seats[0])

        resp = _hold(Client(), guest_event_with_tickets, seats[0])

        assert resp.status_code == 409
        assert resp.json()["guest_hold_token"]

    def test_authenticated_hold_has_no_token(
        self,
        member_client: Client,
        guest_event_with_tickets: Event,
        seated_tier: tuple[TicketTier, list[VenueSeat]],
    ) -> None:
        _, seats = seated_tier
        resp = _hold(member_client, guest_event_with_tickets, seats[0])

        assert resp.status_code == 200, resp.content
        assert resp.json()["guest_hold_token"] is None
        assert GUEST_HOLD_COOKIE not in resp.cookies

    def test_release_response_has_no_token(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        resp = Client().delete(f"/api/events/{guest_event_with_tickets.id}/seating/holds")

        assert resp.status_code == 200
        assert resp.json()["guest_hold_token"] is None


@pytest.mark.django_db(transaction=True)
class TestHeaderOnlyFlow:
    """No cookie anywhere: this is what a browser on a tenant host looks like.

    ``transaction=True`` because guest checkout dispatches its email via ``transaction.on_commit``.
    """

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_hold_then_checkout_then_confirm_with_header_only(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        seated_tier: tuple[TicketTier, list[VenueSeat]],
    ) -> None:
        tier, seats = seated_tier
        token = _hold(Client(), guest_event_with_tickets, seats[0]).json()["guest_hold_token"]
        hold = SeatHold.objects.get(event=guest_event_with_tickets, seat=seats[0])

        # Checkout binds the hold session into the emailed confirmation token only when it sees it.
        assert _checkout(Client(), guest_event_with_tickets, tier, seats[0]).status_code == 200
        assert _emailed_token_session(mock_send_email) is None
        mock_send_email.reset_mock()
        ok = _checkout(Client(), guest_event_with_tickets, tier, seats[0], **{HEADER_KEY: token})
        assert ok.status_code == 200, ok.content
        assert _emailed_token_session(mock_send_email) == hold.guest_session

        # Opening the emailed link on another device (no cookie, no header) consumes the hold.
        confirm = Client().post(
            reverse("api:confirm_guest_action"),
            data={"token": mock_send_email.call_args[0][1]},
            content_type="application/json",
        )
        assert confirm.status_code == 200, confirm.content
        assert (
            Ticket.objects.get(user__email="tenantbuyer@example.com", event=guest_event_with_tickets).seat == seats[0]
        )
        assert not SeatHold.objects.filter(event=guest_event_with_tickets, seat=seats[0]).exists()

    def test_availability_with_header_only_reports_own_holds(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        _, seats = seated_tier
        token = _hold(Client(), guest_event_with_tickets, seats[0]).json()["guest_hold_token"]

        mine = _availability(Client(), guest_event_with_tickets, **{HEADER_KEY: token}).json()
        theirs = _availability(Client(), guest_event_with_tickets).json()

        assert mine["my_holds"] == [str(seats[0].id)]
        assert theirs["my_holds"] == []

    def test_confirm_with_header_only_consumes_the_guests_own_hold(
        self,
        guest_event_with_tickets: Event,
        seated_tier: tuple[TicketTier, list[VenueSeat]],
        existing_guest_user: RevelUser,
    ) -> None:
        tier, seats = seated_tier
        token = _hold(Client(), guest_event_with_tickets, seats[0]).json()["guest_hold_token"]
        # A confirmation token that does not itself carry the hold session (the header must supply it).
        confirm_token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            tier.id,
            [schema.TicketPurchaseItem(guest_name="Header Confirm", seat_id=seats[0].id)],
        )
        url = reverse("api:confirm_guest_action")

        blocked = Client().post(url, data={"token": confirm_token}, content_type="application/json")
        assert blocked.status_code != 200, blocked.content

        ok = Client().post(url, data={"token": confirm_token}, content_type="application/json", **{HEADER_KEY: token})
        assert ok.status_code == 200, ok.content
        assert Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets).seat == seats[0]
        assert not SeatHold.objects.filter(event=guest_event_with_tickets, seat=seats[0]).exists()

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_cookie_only_still_works(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        seated_tier: tuple[TicketTier, list[VenueSeat]],
    ) -> None:
        """Regression: the platform path (cookie, no header) is unchanged."""
        tier, seats = seated_tier
        client = Client()
        assert _hold(client, guest_event_with_tickets, seats[0]).status_code == 200  # Client keeps the cookie

        ok = _checkout(client, guest_event_with_tickets, tier, seats[0])

        assert ok.status_code == 200, ok.content
        mock_send_email.assert_called_once()


class TestHeaderResolution:
    def test_invalid_header_is_treated_like_an_invalid_cookie(
        self, guest_event_with_tickets: Event, seated_tier: tuple[TicketTier, list[VenueSeat]]
    ) -> None:
        _, seats = seated_tier
        owner_token = _hold(Client(), guest_event_with_tickets, seats[0]).json()["guest_hold_token"]
        tampered = owner_token[:-4] + "AAAA"

        via_header = _availability(Client(), guest_event_with_tickets, **{HEADER_KEY: tampered})
        cookie_client = Client()
        cookie_client.cookies[GUEST_HOLD_COOKIE] = tampered
        via_cookie = _availability(cookie_client, guest_event_with_tickets)
        anonymous = _availability(Client(), guest_event_with_tickets)

        assert via_header.status_code == via_cookie.status_code == 200
        assert via_header.json() == via_cookie.json() == anonymous.json()  # no session adopted
        assert via_header.json()["my_holds"] == []
        # A hold with the bad header mints a fresh session instead of adopting the tampered one.
        resp = _hold(Client(), guest_event_with_tickets, seats[1], **{HEADER_KEY: tampered})
        assert resp.status_code == 200
        assert resp.json()["guest_hold_token"] not in (None, tampered)

    def test_header_takes_precedence_over_cookie(self) -> None:
        _, header_token = issue_guest_hold_token()
        _, cookie_token = issue_guest_hold_token()
        client = Client()
        client.cookies[GUEST_HOLD_COOKIE] = cookie_token

        hold = resolve_request_guest_hold(client.get("/api/", headers={GUEST_HOLD_HEADER: header_token}).wsgi_request)

        assert hold is not None and hold.token == header_token

    def test_invalid_header_falls_back_to_a_valid_cookie(self) -> None:
        _, cookie_token = issue_guest_hold_token()
        client = Client()
        client.cookies[GUEST_HOLD_COOKIE] = cookie_token

        hold = resolve_request_guest_hold(client.get("/api/", headers={GUEST_HOLD_HEADER: "garbage"}).wsgi_request)

        assert hold is not None and hold.token == cookie_token

    def test_no_header_no_cookie_resolves_nothing(self) -> None:
        assert resolve_request_guest_hold(Client().get("/api/").wsgi_request) is None

    def test_header_name_matches_the_wsgi_key_used_in_these_tests(self) -> None:
        assert f"HTTP_{GUEST_HOLD_HEADER.upper().replace('-', '_')}" == HEADER_KEY
