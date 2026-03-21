# src/accounts/tests/test_controllers/test_referral_controller.py
"""Integration tests for the ReferralController."""

import typing as t

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import ReferralCode, RevelUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def referrer(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referrer@example.com",
        email="referrer@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def active_referral_code(referrer: RevelUser) -> ReferralCode:
    return ReferralCode.objects.create(user=referrer, code="ACTIVE123")


@pytest.fixture
def inactive_referral_code(referrer: RevelUser) -> ReferralCode:
    return ReferralCode.objects.create(user=referrer, code="INACTIVE1", is_active=False)


def test_validate_active_code(client: Client, active_referral_code: ReferralCode) -> None:
    """Test that a valid, active referral code returns 200."""
    url = reverse("api:validate-referral-code")
    response = client.get(url, {"code": "ACTIVE123"})

    assert response.status_code == 200
    assert response.json() == {"valid": True}


def test_validate_case_insensitive(client: Client, active_referral_code: ReferralCode) -> None:
    """Test that lookup is case-insensitive."""
    url = reverse("api:validate-referral-code")
    response = client.get(url, {"code": "active123"})

    assert response.status_code == 200
    assert response.json() == {"valid": True}


def test_validate_inactive_code(client: Client, inactive_referral_code: ReferralCode) -> None:
    """Test that an inactive code returns 404."""
    url = reverse("api:validate-referral-code")
    response = client.get(url, {"code": "INACTIVE1"})

    assert response.status_code == 404


def test_validate_nonexistent_code(client: Client) -> None:
    """Test that a nonexistent code returns 404."""
    url = reverse("api:validate-referral-code")
    response = client.get(url, {"code": "NOSUCHCODE"})

    assert response.status_code == 404


def test_validate_missing_code_param(client: Client) -> None:
    """Test that missing code query param returns 422."""
    url = reverse("api:validate-referral-code")
    response = client.get(url)

    assert response.status_code == 422
