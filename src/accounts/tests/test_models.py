"""test_models.py: Unit tests for the accounts models."""

import pytest

from accounts.models import RevelUser, RevelUserQueryset

pytestmark = pytest.mark.django_db


def test_reveluser_clean_normalizes_phone_number() -> None:
    """Test that RevelUser.clean() normalizes the phone number."""
    user = RevelUser(username="test_user", phone_number="+39 328 (125)62-1", password="<PASSWORD>")
    user.save()
    assert user.phone_number == "+39328125621"


def test_reveluser_save_calls_clean() -> None:
    """Test that RevelUser.save() calls clean()."""
    user = RevelUser(username="test_user", phone_number="+39 328 (125)62-1", password="<PASSWORD>")
    user.save()
    assert user.phone_number == "+39328125621"


def test_reveluser_creation_with_default_values() -> None:
    """Test that a RevelUser can be created with default values."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    assert user.phone_number is None
    assert user.email_verified is False
    assert user.totp_active is False
    assert len(user.totp_secret) > 0  # Should have a default TOTP secret


def test_reveluser_manager_get_queryset() -> None:
    """Test that RevelUserManager.get_queryset() returns a RevelUserQueryset."""
    queryset = RevelUser.objects.get_queryset()
    assert isinstance(queryset, RevelUserQueryset)


def test_reveluser_get_display_name_with_preferred_name() -> None:
    """Test that get_display_name() returns preferred_name when available."""
    user = RevelUser.objects.create_user(
        username="test_user", first_name="John", last_name="Doe", preferred_name="Johnny", password="password"
    )
    assert user.get_display_name() == "Johnny"


def test_reveluser_get_display_name_without_preferred_name() -> None:
    """Test that get_display_name() returns full name when preferred_name is not set."""
    user = RevelUser.objects.create_user(username="test_user", first_name="John", last_name="Doe", password="password")
    assert user.get_display_name() == "John Doe"


def test_reveluser_get_display_name_empty_preferred_name() -> None:
    """Test that get_display_name() returns full name when preferred_name is empty string."""
    user = RevelUser.objects.create_user(
        username="test_user", first_name="John", last_name="Doe", preferred_name="", password="password"
    )
    assert user.get_display_name() == "John Doe"


def test_reveluser_get_display_name_no_names() -> None:
    """Test that get_display_name() returns username when no other names are available."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    assert user.get_display_name() == "test_user"
