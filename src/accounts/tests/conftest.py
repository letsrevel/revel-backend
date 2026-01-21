# src/accounts/tests/conftest.py
import typing as t

import pytest
from django.test.client import Client
from django_google_sso.models import GoogleSSOUser
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.models import RevelUser


@pytest.fixture
def valid_register_payload() -> schema.RegisterUserSchema:
    """Provides a valid payload for the user registration endpoint."""
    return schema.RegisterUserSchema(
        email="newuser@example.com",
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="New",
        last_name="User",
        accept_toc_and_privacy=True,
    )


@pytest.fixture
def user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A standard, non-privileged user."""
    return django_user_model.objects.create_user(
        username="testuser@example.com",
        email="testuser@example.com",
        password="strong-password-123!",
        first_name="Test",
        last_name="User",
    )


@pytest.fixture
def totp_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user with TOTP enabled."""
    return django_user_model.objects.create_user(
        username="totpuser@example.com",
        email="totpuser@example.com",
        password="strong-password-123!",
        totp_active=True,
    )


@pytest.fixture
def google_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user who signed up via Google SSO."""
    user = django_user_model.objects.create_user(
        username="googleuser@example.com",
        email="googleuser@example.com",
        password="<GOOGLE_SSO_USER>",
        email_verified=True,
    )
    GoogleSSOUser.objects.create(user=user, google_id="123456789")
    return user


@pytest.fixture
def unverified_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user whose email is not yet verified."""
    return django_user_model.objects.create_user(
        username="unverified@example.com",
        email="unverified@example.com",
        password="strong-password-123!",
        email_verified=False,
    )


@pytest.fixture
def auth_client(user: RevelUser) -> Client:
    """An API client authenticated as the standard user."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def unverified_auth_client(unverified_user: RevelUser) -> Client:
    """An API client authenticated as the unverified user."""
    refresh = RefreshToken.for_user(unverified_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user with staff privileges."""
    return django_user_model.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="strong-password-123!",
        is_staff=True,
    )


@pytest.fixture
def superuser(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A superuser with full admin privileges."""
    return django_user_model.objects.create_superuser(
        username="superuser@example.com",
        email="superuser@example.com",
        password="strong-password-123!",
        first_name="Super",
        last_name="Admin",
    )


@pytest.fixture
def another_superuser(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """Another superuser for testing impersonation restrictions."""
    return django_user_model.objects.create_superuser(
        username="another_superuser@example.com",
        email="another_superuser@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def inactive_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """An inactive user account."""
    return django_user_model.objects.create_user(
        username="inactive@example.com",
        email="inactive@example.com",
        password="strong-password-123!",
        is_active=False,
    )
