# src/telegram/models.py

import typing as t

from django.conf import settings
from django.db import models

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
    """Links a Django User to a Telegram User ID."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_user")
    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    telegram_username = models.CharField(max_length=100, blank=True, null=True)
    blocked_by_user = models.BooleanField(
        default=False, db_index=True, help_text="True if the user has blocked the bot."
    )
    user_is_deactivated = models.BooleanField(
        default=False, db_index=True, help_text="True if the user has deactivated their account."
    )
    was_welcomed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TelegramUserManager()

    class Meta:
        indexes = [
            models.Index(fields=["blocked_by_user", "user_is_deactivated"], name="tg_user_blocked_deact_idx"),
        ]
