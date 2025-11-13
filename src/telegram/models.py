# src/telegram/models.py

import secrets
import typing as t
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel


class TelegramUserQuerySet(models.QuerySet["TelegramUser"]):
    """Custom QuerySet for TelegramUser."""

    def with_user(self) -> t.Self:
        """Returns a queryset with related user."""
        return self.select_related(
            "user",
        ).prefetch_related(models.Prefetch("user__groups"))

    def active_users(self) -> t.Self:
        """Returns a queryset with active users."""
        return self.with_user().filter(blocked_by_user=False, user_is_deactivated=False, user__is_active=True)


class TelegramUserManager(models.Manager["TelegramUser"]):
    """Custom manager for TelegramUser."""

    def get_queryset(self) -> TelegramUserQuerySet:
        """Returns a custom QuerySet."""
        return TelegramUserQuerySet(self.model, using=self._db).with_user()

    def active_users(self) -> TelegramUserQuerySet:
        """Returns a queryset with active users."""
        return self.get_queryset().active_users()


class TelegramUser(TimeStampedModel):
    """Links a Django User to a Telegram User ID.

    A TelegramUser can exist without being linked to a RevelUser initially.
    Account linking is done via OTP confirmation flow.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="telegram_users",
        null=True,
        blank=True,
        help_text="Linked Revel user account (null until account linking is complete)",
    )
    telegram_id = models.BigIntegerField(unique=True)
    telegram_username = models.CharField(max_length=100, blank=True, null=True)
    blocked_by_user = models.BooleanField(default=False, help_text="True if the user has blocked the bot.")
    user_is_deactivated = models.BooleanField(
        default=False, help_text="True if the user has deactivated their account."
    )
    was_welcomed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TelegramUserManager()

    class Meta:
        indexes = [
            models.Index(fields=["blocked_by_user", "user_is_deactivated"], name="tg_user_blocked_deact_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.telegram_username or 'Unknown'} ({self.telegram_id})"


def get_otp_expiration_time() -> t.Any:
    """Returns the expiration time for OTPs."""
    return timezone.now() + timedelta(minutes=settings.TELEGRAM_OTP_EXPIRATION_MINUTES)


def generate_otp() -> str:
    """Generates a random 9-digit OTP."""
    return f"{secrets.randbelow(10**9):09d}"


class AccountOTP(TimeStampedModel):
    """Stores OTP for telegram account linking.

    Used in the flow:
    1. User requests /connect in Telegram bot
    2. Bot creates AccountOTP record with 9-digit code
    3. User enters code in Revel app
    4. App validates OTP and links accounts
    """

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    tg_user = models.OneToOneField(TelegramUser, on_delete=models.CASCADE, related_name="account_otp")
    otp = models.CharField(max_length=9, default=generate_otp, unique=True)
    used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(default=get_otp_expiration_time, db_index=True)

    def is_expired(self) -> bool:
        """Checks if the OTP is expired."""
        return self.expires_at < timezone.now()

    def __str__(self) -> str:
        return f"OTP for {self.tg_user}"
