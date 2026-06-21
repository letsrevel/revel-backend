import typing as t

import pytest

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference
from notifications.service.dispatcher import determine_delivery_channels


@pytest.mark.django_db
def test_telegram_stripped_when_flag_off(settings: t.Any, regular_user: RevelUser) -> None:
    """Telegram must never appear in resolved channels when the flag is off."""
    settings.FEATURE_TELEGRAM = False
    prefs = regular_user.notification_preferences
    prefs.digest_frequency = NotificationPreference.DigestFrequency.IMMEDIATE
    prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM]
    prefs.save()

    channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

    assert DeliveryChannel.TELEGRAM not in channels
    assert DeliveryChannel.IN_APP in channels


@pytest.mark.django_db
def test_telegram_kept_when_flag_on(settings: t.Any, regular_user: RevelUser) -> None:
    """Sanity check: Telegram is resolved when the flag is on (so the strip is meaningful)."""
    settings.FEATURE_TELEGRAM = True
    prefs = regular_user.notification_preferences
    prefs.digest_frequency = NotificationPreference.DigestFrequency.IMMEDIATE
    prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM]
    prefs.save()

    channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

    assert DeliveryChannel.TELEGRAM in channels
