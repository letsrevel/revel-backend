"""test_models.py: Unit tests for the accounts models."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from accounts.models import (
    AccountOTP,
    RevelUser,
    RevelUserQueryset,
    generate_otp,
    get_12h_otp_expiration_time,
    get_or_create_user_otp,
    get_otp_expiration_time,
)

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


def test_accountotp_is_expired() -> None:
    """Test that AccountOTP.is_expired() correctly determines if an OTP is expired."""
    user = RevelUser.objects.create_user(username="test_user", password="password")

    # Create an expired OTP
    expired_otp = AccountOTP.objects.create(user=user, expires_at=timezone.now() - timedelta(minutes=1))
    assert expired_otp.is_expired() is True
    expired_otp.delete()

    # Create a valid OTP
    valid_otp = AccountOTP.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=1))
    assert valid_otp.is_expired() is False


def test_accountotp_str() -> None:
    """Test that AccountOTP.__str__() returns the expected string."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    otp = AccountOTP.objects.create(user=user)
    assert str(otp) == f"OTP for {user.username}"


def test_generate_otp() -> None:
    """Test that generate_otp() returns a 6-digit string."""
    otp = generate_otp()
    assert isinstance(otp, str)
    assert len(otp) == 6
    assert otp.isdigit()


def test_get_otp_expiration_time() -> None:
    """Test that get_otp_expiration_time() returns a future datetime."""
    expiration_time = get_otp_expiration_time()
    assert isinstance(expiration_time, datetime)
    assert expiration_time > timezone.now()


def test_get_12h_otp_expiration_time() -> None:
    """Test that get_12h_otp_expiration_time() returns a datetime 12 hours in the future."""
    expiration_time = get_12h_otp_expiration_time()
    assert isinstance(expiration_time, datetime)
    assert expiration_time > timezone.now()

    # Check that it's approximately 12 hours in the future
    time_diff = expiration_time - timezone.now()
    assert 11.9 <= time_diff.total_seconds() / 3600 <= 12.1


def test_get_or_create_user_otp_creates_new() -> None:
    """Test that get_or_create_user_otp() creates a new OTP when none exists."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    otp = get_or_create_user_otp(user)
    assert isinstance(otp, AccountOTP)
    assert otp.user == user
    assert not otp.is_expired()


def test_get_or_create_user_otp_returns_existing() -> None:
    """Test that get_or_create_user_otp() returns an existing OTP if it's not expired."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    original_otp = AccountOTP.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=10))
    retrieved_otp = get_or_create_user_otp(user)
    assert retrieved_otp.id == original_otp.id


def test_get_or_create_user_otp_replaces_expired() -> None:
    """Test that get_or_create_user_otp() replaces an expired OTP."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    expired_otp = AccountOTP.objects.create(user=user, expires_at=timezone.now() - timedelta(minutes=1))
    new_otp = get_or_create_user_otp(user)
    assert new_otp.id != expired_otp.id
    assert not AccountOTP.objects.filter(id=expired_otp.id).exists()


def test_get_or_create_user_otp_with_long_expiration() -> None:
    """Test that get_or_create_user_otp() with long_expiration=True creates an OTP with a 12-hour expiration."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    otp = get_or_create_user_otp(user, long_expiration=True)

    # Check that expiration is approximately 12 hours in the future
    time_diff = otp.expires_at - timezone.now()
    assert 11.9 <= time_diff.total_seconds() / 3600 <= 12.1


def test_get_or_create_user_otp_replaces_short_expiration_with_long() -> None:
    """Test that get_or_create_user_otp() replaces a short-expiration OTP with a long-expiration one when requested."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    short_otp = AccountOTP.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=10))
    long_otp = get_or_create_user_otp(user, long_expiration=True)
    assert long_otp.id != short_otp.id

    # Check that expiration is approximately 12 hours in the future
    time_diff = long_otp.expires_at - timezone.now()
    assert 11.9 <= time_diff.total_seconds() / 3600 <= 12.1


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
    """Test that get_display_name() returns empty string when no names are available."""
    user = RevelUser.objects.create_user(username="test_user", password="password")
    assert user.get_display_name() == ""
