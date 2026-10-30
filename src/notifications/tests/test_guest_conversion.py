"""``reset_converted_guest_preferences``: converted guests get the regular defaults (#1248)."""

import pytest

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.service.preferences import reset_converted_guest_preferences

pytestmark = pytest.mark.django_db


def _settings(user: RevelUser) -> dict[str, object]:
    return dict(NotificationPreference.objects.get(user=user).notification_type_settings)


def _set(user: RevelUser, key: NotificationType, value: dict[str, object]) -> None:
    prefs = NotificationPreference.objects.get(user=user)
    prefs.notification_type_settings[str(key)] = value
    prefs.save()


def test_guest_row_becomes_regular_defaults(guest_user: RevelUser) -> None:
    assert reset_converted_guest_preferences(guest_user) is True

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_second_run_is_noop(guest_user: RevelUser) -> None:
    reset_converted_guest_preferences(guest_user)

    assert reset_converted_guest_preferences(guest_user) is False
    assert _settings(guest_user) == get_default_notification_type_settings()


def test_explicit_values_are_preserved(guest_user: RevelUser) -> None:
    explicit = {"enabled": True, "channels": ["email"]}
    _set(guest_user, NotificationType.ORG_ANNOUNCEMENT, explicit)

    reset_converted_guest_preferences(guest_user)

    expected = {**get_default_notification_type_settings(), NotificationType.ORG_ANNOUNCEMENT: explicit}
    assert _settings(guest_user) == expected


@pytest.mark.parametrize("channels", [[], ["in_app"], ["email", "in_app"]])
def test_disabled_entry_is_reset_whatever_its_channels(guest_user: RevelUser, channels: list[str]) -> None:
    _set(guest_user, NotificationType.ORG_ANNOUNCEMENT, {"enabled": False, "channels": channels})
    _set(guest_user, NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, {"enabled": False, "channels": channels})

    reset_converted_guest_preferences(guest_user)

    settings = _settings(guest_user)
    assert NotificationType.ORG_ANNOUNCEMENT not in settings
    assert settings == get_default_notification_type_settings()


def test_pinned_defaults_are_restored_not_dropped(guest_user: RevelUser) -> None:
    reset_converted_guest_preferences(guest_user)

    settings = _settings(guest_user)
    assert settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED] == {
        "enabled": True,
        "channels": ["in_app", "telegram"],
    }
    assert NotificationType.ORGANIZATION_FOLLOWED in settings
    assert NotificationType.MEMBERSHIP_GRANTED not in settings


def test_missing_row_is_created_with_regular_defaults(guest_user: RevelUser) -> None:
    NotificationPreference.objects.filter(user=guest_user).delete()
    RevelUser.objects.filter(pk=guest_user.pk).update(guest=False)
    guest_user.refresh_from_db()

    assert reset_converted_guest_preferences(guest_user) is True

    assert _settings(guest_user) == get_default_notification_type_settings()
