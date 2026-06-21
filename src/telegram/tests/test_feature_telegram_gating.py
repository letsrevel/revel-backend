import typing as t

import pytest
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser


@pytest.fixture
def auth_client(django_user_model: t.Type[RevelUser]) -> Client:
    """API client authenticated as a standard user."""
    user = django_user_model.objects.create_user(
        username="tguser@example.com", email="tguser@example.com", password="pass"
    )
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_status_endpoint_404_when_telegram_disabled(settings: t.Any, auth_client: Client) -> None:
    settings.FEATURE_TELEGRAM = False
    resp = auth_client.get("/api/telegram/status")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_status_endpoint_ok_when_telegram_enabled(settings: t.Any, auth_client: Client) -> None:
    settings.FEATURE_TELEGRAM = True
    resp = auth_client.get("/api/telegram/status")
    assert resp.status_code == 200
