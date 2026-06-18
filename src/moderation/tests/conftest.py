import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser


@pytest.fixture
def auth_client(user: RevelUser) -> Client:
    """An API client authenticated as the standard user."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
