"""Signal handlers for Telegram account lifecycle events.

Listens to signals from the telegram app and updates the user's
notification preferences accordingly.
"""

import typing as t

import structlog
from django.dispatch import receiver

from notifications.enums import DeliveryChannel
from notifications.models import NotificationPreference
from telegram.signals import telegram_account_linked, telegram_account_unlinked

if t.TYPE_CHECKING:
    from accounts.models import RevelUser
    from telegram.models import TelegramUser

logger = structlog.get_logger(__name__)


@receiver(telegram_account_linked)
def enable_telegram_channel(sender: type, user: "RevelUser", telegram_user: "TelegramUser", **kwargs: object) -> None:
    """Add TELEGRAM to enabled_channels when user links their Telegram account."""
    prefs = NotificationPreference.objects.get(user=user)

    if DeliveryChannel.TELEGRAM not in prefs.enabled_channels:
        prefs.enabled_channels.append(DeliveryChannel.TELEGRAM)
        prefs.save(update_fields=["enabled_channels", "updated_at"])
        logger.info("telegram_channel_enabled", user_id=str(user.id))


@receiver(telegram_account_unlinked)
def disable_telegram_channel(sender: type, user: "RevelUser", telegram_user: "TelegramUser", **kwargs: object) -> None:
    """Remove TELEGRAM from enabled_channels when user unlinks their Telegram account."""
    prefs = NotificationPreference.objects.get(user=user)

    if DeliveryChannel.TELEGRAM in prefs.enabled_channels:
        prefs.enabled_channels.remove(DeliveryChannel.TELEGRAM)
        prefs.save(update_fields=["enabled_channels", "updated_at"])
        logger.info("telegram_channel_disabled", user_id=str(user.id))
