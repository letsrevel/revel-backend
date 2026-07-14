"""Tests for the series-pass reserve -> checkout-session flow (#632).

Covers: `checkout_series_pass` reserves (requires_payment + reservation_id) then
`series_pass_checkout_session` creates the Stripe session. Ownership is enforced
by scoping to ``Payment(reservation_id=..., user=...)``.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventSeries, Organization, SeriesPass, SeriesPassTierLink, TicketTier

pytestmark = pytest.mark.django_db


def _fake_session(session_id: str = "cs_series_test123") -> mock.Mock:
    """A minimal stand-in for a ``stripe.checkout.Session``."""
    return mock.Mock(id=session_id, url=f"https://checkout.stripe.com/c/{session_id}")


@pytest.fixture
def revel_user_client(revel_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(revel_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="other_session_holder@example.com", email="other_session_holder@example.com")


@pytest.fixture
def other_user_client(other_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(other_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def online_pass(stripe_connected_organization: Organization, event_series: EventSeries) -> SeriesPass:
    """An ONLINE series pass covering 2 future events (the quote requires >= 2 remaining)."""
    stripe_connected_organization.visibility = Organization.Visibility.PUBLIC
    stripe_connected_organization.save(update_fields=["visibility"])
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Session Endpoint Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    for i in range(2):
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name=f"Session Endpoint Event {i}",
            slug=f"session-endpoint-event-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now() + timedelta(days=i + 1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name=f"Session Endpoint Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


class TestSeriesPassCheckoutSession:
    def test_reserve_returns_requires_payment_and_reservation_id(
        self, revel_user_client: Client, online_pass: SeriesPass
    ) -> None:
        """Checkout on an ONLINE pass reserves and returns a reservation_id, no Stripe call."""
        url = reverse("api:checkout_series_pass", kwargs={"pass_id": online_pass.id})
        with mock.patch("stripe.checkout.Session.create") as mock_create:
            response = revel_user_client.post(url, data=b"", content_type="application/json")
            mock_create.assert_not_called()

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is True
        assert body["reservation_id"]
        assert body["checkout_url"] is None
        assert body["held_pass"]["status"] == "pending"

    def test_checkout_session_returns_stripe_url(self, revel_user_client: Client, online_pass: SeriesPass) -> None:
        """POSTing checkout-session for an owned reservation creates the Stripe session."""
        reserve_url = reverse("api:checkout_series_pass", kwargs={"pass_id": online_pass.id})
        with mock.patch("stripe.checkout.Session.create"):
            reserve_response = revel_user_client.post(reserve_url, data=b"", content_type="application/json")
        reservation_id = reserve_response.json()["reservation_id"]

        fake = _fake_session()
        session_url = reverse("api:series_pass_checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = revel_user_client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        assert session_response.status_code == 200, session_response.content
        assert session_response.json()["checkout_url"] == fake.url

    def test_checkout_session_for_other_users_reservation_is_404(
        self, revel_user_client: Client, other_user_client: Client, online_pass: SeriesPass
    ) -> None:
        """A reservation owned by one user cannot be sessioned by another."""
        reserve_url = reverse("api:checkout_series_pass", kwargs={"pass_id": online_pass.id})
        with mock.patch("stripe.checkout.Session.create"):
            reserve_response = revel_user_client.post(reserve_url, data=b"", content_type="application/json")
        reservation_id = reserve_response.json()["reservation_id"]

        session_url = reverse("api:series_pass_checkout_session", kwargs={"reservation_id": reservation_id})
        response = other_user_client.post(session_url, content_type="application/json")

        assert response.status_code == 404

    def test_checkout_session_for_unknown_reservation_is_404(self, revel_user_client: Client) -> None:
        """An unrecognized reservation_id (never reserved) returns 404."""
        session_url = reverse("api:series_pass_checkout_session", kwargs={"reservation_id": uuid.uuid4()})
        response = revel_user_client.post(session_url, content_type="application/json")

        assert response.status_code == 404
