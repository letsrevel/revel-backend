"""Tests for mandatory-type channels, disable_email_for_type and enable_channel (#1030)."""

import pytest

from accounts.models import RevelUser
from notifications.enums import MANDATORY_TYPES, TRANSACTIONAL_TYPES, DeliveryChannel, NotificationType
from notifications.models import NotificationPreference
from notifications.service import dispatcher

pytestmark = pytest.mark.django_db

IN_APP = DeliveryChannel.IN_APP
EMAIL = DeliveryChannel.EMAIL
TELEGRAM = DeliveryChannel.TELEGRAM


@pytest.fixture
def prefs(user: RevelUser) -> NotificationPreference:
    p = NotificationPreference.objects.get(user=user)
    p.enabled_channels = [IN_APP, EMAIL]
    return p


def test_mandatory_types_contents() -> None:
    assert TRANSACTIONAL_TYPES < MANDATORY_TYPES
    assert NotificationType.ACCOUNT_BANNED in MANDATORY_TYPES
    assert NotificationType.SYSTEM_ANNOUNCEMENT in MANDATORY_TYPES
    # Kept importable from the dispatcher for existing callers.
    assert dispatcher.TRANSACTIONAL_TYPES is TRANSACTIONAL_TYPES


class TestMandatoryTypeChannels:
    @pytest.mark.parametrize("notification_type", sorted(MANDATORY_TYPES))
    def test_silence_does_not_block(self, prefs: NotificationPreference, notification_type: str) -> None:
        prefs.silence_all_notifications = True
        assert prefs.get_channels_for_notification_type(notification_type) == [IN_APP, EMAIL]

    @pytest.mark.parametrize("notification_type", sorted(MANDATORY_TYPES))
    def test_email_off_globally_still_emails(self, prefs: NotificationPreference, notification_type: str) -> None:
        prefs.enabled_channels = []
        assert prefs.get_channels_for_notification_type(notification_type) == [IN_APP, EMAIL]

    @pytest.mark.parametrize("notification_type", sorted(MANDATORY_TYPES))
    def test_type_disabled_still_delivers(self, prefs: NotificationPreference, notification_type: str) -> None:
        prefs.notification_type_settings[notification_type] = {"enabled": False, "channels": [IN_APP]}
        assert prefs.get_channels_for_notification_type(notification_type) == [IN_APP, EMAIL]

    def test_keeps_base_channels_without_duplicates(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [TELEGRAM, EMAIL]
        assert prefs.get_channels_for_notification_type(NotificationType.TICKET_CREATED) == [TELEGRAM, EMAIL, IN_APP]

    def test_non_mandatory_type_still_silenced(self, prefs: NotificationPreference) -> None:
        prefs.silence_all_notifications = True
        assert prefs.get_channels_for_notification_type(NotificationType.EVENT_REMINDER) == []

    def test_non_mandatory_type_disabled(self, prefs: NotificationPreference) -> None:
        prefs.notification_type_settings[NotificationType.EVENT_REMINDER] = {"enabled": False}
        assert prefs.get_channels_for_notification_type(NotificationType.EVENT_REMINDER) == []


class TestDisableEmailForType:
    def test_pins_effective_channels_minus_email(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [IN_APP, EMAIL, TELEGRAM]

        assert prefs.disable_email_for_type(NotificationType.EVENT_REMINDER) is True

        assert prefs.notification_type_settings[NotificationType.EVENT_REMINDER] == {
            "enabled": True,
            "channels": [IN_APP, TELEGRAM],
        }
        assert EMAIL not in prefs.get_channels_for_notification_type(NotificationType.EVENT_REMINDER)
        # Other types untouched.
        assert EMAIL in prefs.get_channels_for_notification_type(NotificationType.EVENT_OPEN)

    def test_never_writes_empty_channel_list(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [EMAIL]

        assert prefs.disable_email_for_type(NotificationType.EVENT_REMINDER) is True

        setting = prefs.notification_type_settings[NotificationType.EVENT_REMINDER]
        assert setting["channels"] == [IN_APP]
        assert prefs.get_channels_for_notification_type(NotificationType.EVENT_REMINDER) == [IN_APP]

    def test_preserves_existing_enabled_flag(self, prefs: NotificationPreference) -> None:
        prefs.notification_type_settings[NotificationType.EVENT_REMINDER] = {"enabled": False}

        prefs.disable_email_for_type(NotificationType.EVENT_REMINDER)

        assert prefs.notification_type_settings[NotificationType.EVENT_REMINDER] == {
            "enabled": False,
            "channels": [IN_APP],
        }

    def test_idempotent(self, prefs: NotificationPreference) -> None:
        assert prefs.disable_email_for_type(NotificationType.EVENT_REMINDER) is True
        assert prefs.disable_email_for_type(NotificationType.EVENT_REMINDER) is False

    def test_persists(self, prefs: NotificationPreference) -> None:
        prefs.disable_email_for_type(NotificationType.EVENT_REMINDER)
        prefs.save()
        prefs.refresh_from_db()
        assert prefs.get_channels_for_notification_type(NotificationType.EVENT_REMINDER) == [IN_APP]


class TestEnableChannel:
    def test_adds_to_enabled_channels(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [IN_APP]
        assert prefs.enable_channel(EMAIL) is True
        assert prefs.enabled_channels == [IN_APP, EMAIL]

    def test_returns_false_when_nothing_changes(self, prefs: NotificationPreference) -> None:
        assert prefs.enable_channel(EMAIL) is False

    def test_restores_telegram_default_for_org_contact(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [IN_APP, EMAIL, TELEGRAM]
        prefs.disable_channel(TELEGRAM)
        assert prefs.notification_type_settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED]["channels"] == [IN_APP]

        assert prefs.enable_channel(TELEGRAM) is True

        assert prefs.get_channels_for_notification_type(NotificationType.ORG_CONTACT_MESSAGE_RECEIVED) == [
            IN_APP,
            TELEGRAM,
        ]

    def test_potluck_stays_in_app_only(self, prefs: NotificationPreference) -> None:
        prefs.enabled_channels = [IN_APP]

        prefs.enable_channel(EMAIL)

        assert prefs.get_channels_for_notification_type(NotificationType.POTLUCK_ITEM_CLAIMED) == [IN_APP]
        assert EMAIL not in prefs.get_channels_for_notification_type(NotificationType.ORG_CONTACT_MESSAGE_RECEIVED)

    def test_override_without_channels_left_alone(self, prefs: NotificationPreference) -> None:
        prefs.notification_type_settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED] = {"enabled": False}

        prefs.enable_channel(TELEGRAM)

        assert prefs.notification_type_settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED] == {"enabled": False}
