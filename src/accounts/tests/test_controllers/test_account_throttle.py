"""Authenticated account/OTP endpoints must be rate limited per user.

The account and OTP controllers used to set ``throttle=AuthThrottle()`` at class
level. That is an *anonymous* (IP-keyed) throttle which returns no cache key for
authenticated requests, so every authenticated route without its own throttle was
effectively unlimited, including TOTP verification.
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _enable_throttling(settings: t.Any) -> None:
    """The local ``.env`` may disable throttling; these tests need it live."""
    settings.DISABLE_THROTTLING = False


def test_account_me_is_throttled_per_user(auth_client: Client) -> None:
    url = reverse("api:me")

    for _ in range(100):
        assert auth_client.get(url).status_code == 200

    assert auth_client.get(url).status_code == 429


def test_otp_verify_is_throttled_per_user(auth_client: Client) -> None:
    """Brute-forcing a 6-digit TOTP code must hit the per-user limit."""
    url = reverse("api:enable-otp")

    for _ in range(100):
        response = auth_client.post(url, {"otp": "000000"}, content_type="application/json")
        assert response.status_code == 403

    response = auth_client.post(url, {"otp": "000000"}, content_type="application/json")
    assert response.status_code == 429
