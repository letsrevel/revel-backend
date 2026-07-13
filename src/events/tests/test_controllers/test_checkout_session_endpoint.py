"""Tests for the reserve -> checkout-session flow (#632).

Covers:
- Authed: ``ticket_checkout`` reserves (requires_payment + reservation_id) then
  ``checkout_session`` creates the Stripe session. Ownership is enforced by
  scoping to ``Payment(reservation_id=..., user=...)``.
- Guest: ``guest_ticket_checkout`` reserves, ``guest_checkout_session`` (the
  ``/public`` variant) creates the session with no user scoping - the
  unguessable reservation_id UUID is the bearer capability.
"""

import uuid
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Organization, TicketTier

pytestmark = pytest.mark.django_db


def _fake_session(session_id: str = "cs_test123") -> mock.Mock:
    """A minimal stand-in for a ``stripe.checkout.Session``."""
    return mock.Mock(id=session_id, url=f"https://checkout.stripe.com/c/{session_id}")


@pytest.fixture
def online_tier(event: Event) -> TicketTier:
    """An ONLINE ticket tier on a Stripe-connected organization."""
    org = event.organization
    org.stripe_account_id = "acct_test123"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save()
    return TicketTier.objects.create(
        event=event,
        name="Online Tier",
        price=Decimal("25.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


@pytest.fixture
def guest_online_event(organization: Organization) -> Event:
    """An event that allows guest checkout, on a Stripe-connected organization."""
    org = organization
    org.stripe_account_id = "acct_test456"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save()
    return Event.objects.create(
        organization=org,
        name="Guest Online Event",
        slug="guest-online-event-checkout-session",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now(),
        max_attendees=100,
        can_attend_without_login=True,
        requires_ticket=True,
    )


@pytest.fixture
def guest_online_tier(guest_online_event: Event) -> TicketTier:
    """An ONLINE ticket tier for the guest-friendly event."""
    return TicketTier.objects.create(
        event=guest_online_event,
        name="Guest Online Tier",
        price=Decimal("15.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


class TestAuthedCheckoutSession:
    """Authed reserve -> checkout-session flow."""

    def test_reserve_returns_requires_payment_and_reservation_id(
        self, member_client: Client, event: Event, online_tier: TicketTier
    ) -> None:
        """Checkout on an ONLINE tier reserves and returns a reservation_id, no Stripe call."""
        url = reverse("api:ticket_checkout", kwargs={"event_id": event.id, "tier_id": online_tier.id})
        with mock.patch("stripe.checkout.Session.create") as mock_create:
            response = member_client.post(url, data={"tickets": [{"guest_name": "A"}]}, content_type="application/json")
            mock_create.assert_not_called()

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is True
        assert body["reservation_id"]
        assert body["tickets"] == []
        assert body["checkout_url"] is None

    def test_checkout_session_returns_stripe_url(
        self, member_client: Client, event: Event, online_tier: TicketTier
    ) -> None:
        """POSTing checkout-session for an owned reservation creates the Stripe session."""
        reserve_url = reverse("api:ticket_checkout", kwargs={"event_id": event.id, "tier_id": online_tier.id})
        with mock.patch("stripe.checkout.Session.create"):
            reserve_response = member_client.post(
                reserve_url, data={"tickets": [{"guest_name": "A"}]}, content_type="application/json"
            )
        reservation_id = reserve_response.json()["reservation_id"]

        fake = _fake_session()
        session_url = reverse("api:checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = member_client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        assert session_response.status_code == 200, session_response.content
        assert session_response.json()["checkout_url"] == fake.url

    def test_checkout_session_for_other_users_reservation_is_404(
        self, member_client: Client, nonmember_client: Client, event: Event, online_tier: TicketTier
    ) -> None:
        """A reservation owned by one user cannot be sessioned by another."""
        reserve_url = reverse("api:ticket_checkout", kwargs={"event_id": event.id, "tier_id": online_tier.id})
        with mock.patch("stripe.checkout.Session.create"):
            reserve_response = member_client.post(
                reserve_url, data={"tickets": [{"guest_name": "A"}]}, content_type="application/json"
            )
        reservation_id = reserve_response.json()["reservation_id"]

        session_url = reverse("api:checkout_session", kwargs={"reservation_id": reservation_id})
        response = nonmember_client.post(session_url, content_type="application/json")

        assert response.status_code == 404

    def test_checkout_session_for_unknown_reservation_is_404(self, member_client: Client) -> None:
        """An unrecognized reservation_id (never reserved) returns 404."""
        session_url = reverse("api:checkout_session", kwargs={"reservation_id": uuid.uuid4()})
        response = member_client.post(session_url, content_type="application/json")

        assert response.status_code == 404


class TestGuestCheckoutSession:
    """Guest reserve -> public checkout-session flow (unguessable bearer UUID)."""

    def test_guest_reserve_then_session(self, guest_online_event: Event, guest_online_tier: TicketTier) -> None:
        """Guest checkout reserves and returns a reservation_id; the public session endpoint sessions it."""
        client = Client()
        reserve_url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_online_event.id, "tier_id": guest_online_tier.id},
        )
        payload = {
            "email": "guest-checkout@example.com",
            "first_name": "Guest",
            "last_name": "User",
            "tickets": [{"guest_name": "Guest User"}],
        }
        with mock.patch("stripe.checkout.Session.create") as mock_create:
            reserve_response = client.post(reserve_url, data=payload, content_type="application/json")
            mock_create.assert_not_called()

        assert reserve_response.status_code == 200, reserve_response.content
        reserve_body = reserve_response.json()
        assert reserve_body["requires_payment"] is True
        reservation_id = reserve_body["reservation_id"]
        assert reservation_id

        fake = _fake_session("cs_guest123")
        session_url = reverse("api:guest_checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        assert session_response.status_code == 200, session_response.content
        assert session_response.json()["checkout_url"] == fake.url
