"""Fixtures for the country-compliance tests."""

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for the ``organization`` fixture's owner."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
