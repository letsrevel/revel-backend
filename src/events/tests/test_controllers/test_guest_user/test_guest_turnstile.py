"""Turnstile gating on guest RSVP and multi-tier checkout (#1387)."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db

EMAIL = "bot@example.com"
BOT_MESSAGE = "Bot verification failed. Please try again."


@pytest.fixture(autouse=True)
def turnstile_on(settings: t.Any) -> None:
    """Enable Turnstile with dummy keys."""
    settings.TURNSTILE_SITE_KEY = "site-key"
    settings.TURNSTILE_SECRET_KEY = "secret-key"


def _siteverify(success: bool) -> MagicMock:
    response = MagicMock()
    response.is_success = True
    response.is_server_error = False
    response.status_code = 200
    response.json.return_value = {"success": True} if success else {"success": False, "error-codes": ["x"]}
    return response


@pytest.fixture
def rsvp_url(guest_event: Event) -> str:
    return reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})


@pytest.fixture
def rsvp_body() -> dict[str, t.Any]:
    return {"email": EMAIL}


@pytest.fixture
def checkout_url(guest_event_with_tickets: Event) -> str:
    return reverse("api:guest_multi_tier_checkout", kwargs={"event_id": guest_event_with_tickets.pk})


@pytest.fixture
def checkout_body(free_tier: TicketTier) -> dict[str, t.Any]:
    return {"email": EMAIL, "items": [{"tier_id": str(free_tier.pk), "tickets": [{"guest_name": "G"}]}]}


@pytest.mark.parametrize("target", ["rsvp", "checkout"])
class TestGuestTurnstile:
    def _target(self, target: str, request: pytest.FixtureRequest) -> tuple[str, dict[str, t.Any]]:
        return request.getfixturevalue(f"{target}_url"), request.getfixturevalue(f"{target}_body")

    def test_missing_token_rejected(self, target: str, request: pytest.FixtureRequest) -> None:
        url, body = self._target(target, request)
        response = Client().post(url, data=body, content_type="application/json")

        assert response.status_code == 400
        assert response.json() == {"detail": BOT_MESSAGE}
        assert not RevelUser.objects.filter(email=EMAIL).exists()

    def test_rejected_token_rejected(self, target: str, request: pytest.FixtureRequest) -> None:
        url, body = self._target(target, request)
        with patch("accounts.service.turnstile.httpx.post", return_value=_siteverify(False)):
            response = Client().post(url, data={**body, "turnstile_token": "bad"}, content_type="application/json")

        assert response.status_code == 400
        assert response.json() == {"detail": BOT_MESSAGE}
        assert not RevelUser.objects.filter(email=EMAIL).exists()

    def test_valid_token_proceeds(self, target: str, request: pytest.FixtureRequest) -> None:
        url, body = self._target(target, request)
        with patch("accounts.service.turnstile.httpx.post", return_value=_siteverify(True)) as mock_post:
            response = Client().post(url, data={**body, "turnstile_token": "good"}, content_type="application/json")

        assert response.status_code == 200
        assert mock_post.call_args.kwargs["data"]["response"] == "good"
        assert RevelUser.objects.filter(email=EMAIL, guest=True).exists()
