import secrets
import typing as t
import uuid
from datetime import datetime, timedelta

import pyotp
from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone
from encrypted_fields.fields import EncryptedTextField

from accounts.validators import normalize_phone_number, validate_phone_number
from common.models import TimeStampedModel


class RevelUserQueryset(models.QuerySet["RevelUser"]):
    """Queryset for RevelUser."""


class RevelUserManager(UserManager["RevelUser"]):
    def get_queryset(self) -> RevelUserQueryset:
        """Get queryset for RevelUser."""
        return RevelUserQueryset(self.model)


class RevelUser(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(
        max_length=20, unique=True, null=True, blank=True, validators=[validate_phone_number], help_text="Phone number"
    )
    preferred_name = models.CharField(db_index=True, max_length=255, blank=True, help_text="Preferred name")
    pronouns = models.CharField(max_length=10, blank=True, help_text="Pronouns")
    email_verified = models.BooleanField(default=False, db_index=True)
    totp_secret = EncryptedTextField(default=pyotp.random_base32, editable=False)
    totp_active = models.BooleanField(default=False, db_index=True)
    language = models.CharField(
        max_length=7,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE,
        db_index=True,
        help_text="User's preferred language",
    )

    objects = RevelUserManager()  # type: ignore[misc]

    class Meta:
        ordering = ["username"]

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save method to call clean()."""
        if self.phone_number:
            self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)

    def get_display_name(self) -> str:
        """Returns the user's preferred name, or their full name as a fallback."""
        return self.preferred_name or self.get_full_name()


def get_otp_expiration_time() -> datetime:
    """Returns the expiration time for OTPs."""
    return timezone.now() + timedelta(minutes=settings.ACCOUNT_OTP_EXPIRATION_MINUTES)


def get_12h_otp_expiration_time() -> datetime:
    """Returns the expiration time for story OTPs (12 hours)."""
    return timezone.now() + timedelta(hours=12)


def generate_otp() -> str:
    """Generates a random 6-digit OTP."""
    return f"{secrets.randbelow(10**6):06d}"


class AccountOTP(TimeStampedModel):
    """Stores the OTP for a user."""

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="telegram_otp")
    otp = models.CharField(max_length=6, default=generate_otp, unique=True)
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(default=get_otp_expiration_time, db_index=True)

    def is_expired(self) -> bool:
        """Checks if the OTP is expired."""
        return self.expires_at < timezone.now()

    def __str__(self) -> str:
        return f"OTP for {self.user.username}"


def get_or_create_user_otp(user: RevelUser, long_expiration: bool = False) -> AccountOTP:
    """Gets or creates an OTP for a user.

    Args:
        user: The user to get or create an OTP for
        long_expiration: If True, the OTP will expire in 12 hours, otherwise in the default time

    Returns:
        The OTP object
    """
    try:
        otp = AccountOTP.objects.get(user=user)
        # If OTP is expired, or we need a long expiration, and it's not set for that, create a new one
        if otp.is_expired() or (long_expiration and otp.expires_at < get_12h_otp_expiration_time()):
            otp.delete()
            raise AccountOTP.DoesNotExist
        return otp
    except AccountOTP.DoesNotExist:
        expiration_func = get_12h_otp_expiration_time if long_expiration else get_otp_expiration_time
        return AccountOTP.objects.create(user=user, expires_at=expiration_func())


class UserDataExport(TimeStampedModel):
    """Stores the user data export."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="data_export")
    file = models.FileField(upload_to="user_data_exports/", null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Data export for {self.user.username}"
