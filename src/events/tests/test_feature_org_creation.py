import typing as t

import pytest
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser


@pytest.fixture
def verified_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A standard user with a verified email (so the email gate passes)."""
    return django_user_model.objects.create_user(
        username="orgmaker@example.com",
        email="orgmaker@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def verified_staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A staff user with a verified email."""
    return django_user_model.objects.create_user(
        username="staffmaker@example.com",
        email="staffmaker@example.com",
        password="pass",
        email_verified=True,
        is_staff=True,
    )


@pytest.fixture
def auth_client(verified_user: RevelUser) -> Client:
    """API client authenticated as the verified standard user."""
    refresh = RefreshToken.for_user(verified_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def staff_auth_client(verified_staff_user: RevelUser) -> Client:
    """API client authenticated as the verified staff user."""
    refresh = RefreshToken.for_user(verified_staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_regular_user_blocked_when_flag_off(settings: t.Any, auth_client: Client) -> None:
    settings.FEATURE_ORGANIZATION_CREATION = False
    resp = auth_client.post(
        "/api/organizations/",
        data={"name": "My Org", "contact_email": "org@example.com"},
        content_type="application/json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_staff_user_allowed_when_flag_off(settings: t.Any, staff_auth_client: Client) -> None:
    settings.FEATURE_ORGANIZATION_CREATION = False
    resp = staff_auth_client.post(
        "/api/organizations/",
        data={"name": "Staff Org", "contact_email": "org@example.com"},
        content_type="application/json",
    )
    assert resp.status_code == 201


@pytest.mark.django_db
def test_regular_user_allowed_when_flag_on(settings: t.Any, auth_client: Client) -> None:
    settings.FEATURE_ORGANIZATION_CREATION = True
    resp = auth_client.post(
        "/api/organizations/",
        data={"name": "Open Org", "contact_email": "org@example.com"},
        content_type="application/json",
    )
    assert resp.status_code == 201
