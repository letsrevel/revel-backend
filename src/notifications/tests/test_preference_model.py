"""Tests for NotificationPreference model helpers."""

import pytest

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference

pytestmark = pytest.mark.django_db


class TestDisableChannel:
    def test_removes_from_enabled_channels(self, user: RevelUser) -> None:
        prefs = NotificationPreference.objects.get(user=user)
        prefs.enabled_channels = [DeliveryChannel.EMAIL, DeliveryChannel.TELEGRAM]

        changed = prefs.disable_channel(DeliveryChannel.TELEGRAM)

        assert changed is True
        assert prefs.enabled_channels == [DeliveryChannel.EMAIL]

    def test_removes_from_per_type_overrides(self, user: RevelUser) -> None:
        """Per-type overrides bypass enabled_channels, so they must be cleared too."""
        prefs = NotificationPreference.objects.get(user=user)
        # Default settings seed ORG_CONTACT_MESSAGE_RECEIVED with IN_APP + TELEGRAM.
        assert DeliveryChannel.TELEGRAM in prefs.get_channels_for_notification_type(
            NotificationType.ORG_CONTACT_MESSAGE_RECEIVED
        )

        changed = prefs.disable_channel(DeliveryChannel.TELEGRAM)

        assert changed is True
        assert DeliveryChannel.TELEGRAM not in prefs.get_channels_for_notification_type(
            NotificationType.ORG_CONTACT_MESSAGE_RECEIVED
        )

    def test_returns_false_when_absent(self, user: RevelUser) -> None:
        prefs = NotificationPreference.objects.get(user=user)
        # Strip any telegram presence (global + per-type defaults) first.
        prefs.disable_channel(DeliveryChannel.TELEGRAM)

        changed = prefs.disable_channel(DeliveryChannel.TELEGRAM)

        assert changed is False
