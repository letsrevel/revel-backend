"""TelegramChannel.can_deliver follows the mandatory-type rule of #1030.

Mandatory types ignore silence and per-type disables (like the dispatcher), so a
silenced user with Telegram among their channels still gets the Telegram delivery the
dispatcher selected. Ordinary types keep the old silence/type checks.
"""

from unittest.mock import MagicMock, patch

import pytest

from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification
from notifications.service.channels.telegram import TelegramChannel

pytestmark = pytest.mark.django_db


def _can_deliver(notification: Notification) -> bool:
    tg_user = MagicMock(telegram_id=123)
    manager = MagicMock()
    manager.filter.return_value.first.return_value = tg_user
    with patch.object(type(notification.user), "telegram_users", new_callable=lambda: property(lambda self: manager)):
        return TelegramChannel().can_deliver(notification)


def _silence_with_telegram(notification: Notification) -> None:
    prefs = notification.user.notification_preferences
    prefs.silence_all_notifications = True
    prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM]
    prefs.save()


def test_mandatory_type_delivers_to_silenced_user(notification: Notification) -> None:
    notification.notification_type = NotificationType.TICKET_CREATED
    _silence_with_telegram(notification)

    assert _can_deliver(notification) is True


def test_mandatory_type_without_telegram_channel_is_not_delivered(notification: Notification) -> None:
    notification.notification_type = NotificationType.TICKET_CREATED
    prefs = notification.user.notification_preferences
    prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]
    prefs.save()

    assert _can_deliver(notification) is False


def test_ordinary_type_still_honours_silence(notification: Notification) -> None:
    notification.notification_type = NotificationType.EVENT_UPDATED
    _silence_with_telegram(notification)

    assert _can_deliver(notification) is False
