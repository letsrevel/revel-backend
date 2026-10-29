"""Guest -> account conversion resets notification preferences to regular defaults (#1248)."""

import pytest

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.service.preferences import reset_converted_guest_preferences

pytestmark = pytest.mark.django_db


def _settings(user: RevelUser) -> dict[str, object]:
    return dict(NotificationPreference.objects.get(user=user).notification_type_settings)


def test_plain_conversion_gives_regular_defaults(guest_user: RevelUser) -> None:
    """The admin path: untick guest and save."""
    guest_user.guest = False
    guest_user.save()

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_explicit_values_are_preserved(guest_user: RevelUser) -> None:
    """A non-guest-default value is a user choice and survives; the rest is reset."""
    explicit = {"enabled": True, "channels": ["email"]}
    prefs = NotificationPreference.objects.get(user=guest_user)
    prefs.notification_type_settings[str(NotificationType.ORG_ANNOUNCEMENT)] = explicit
    prefs.save()

    guest_user.guest = False
    guest_user.save()

    expected = {**get_default_notification_type_settings(), NotificationType.ORG_ANNOUNCEMENT: explicit}
    assert _settings(guest_user) == expected


def test_pinned_defaults_are_restored_not_dropped(guest_user: RevelUser) -> None:
    """Keys with a pinned regular default get it back (not the in_app+email fallback)."""
    guest_user.guest = False
    guest_user.save()

    settings = _settings(guest_user)
    assert settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED] == {
        "enabled": True,
        "channels": ["in_app", "telegram"],
    }
    assert NotificationType.ORGANIZATION_FOLLOWED in settings
    assert NotificationType.MEMBERSHIP_GRANTED not in settings


def test_missing_row_is_created_with_regular_defaults(guest_user: RevelUser) -> None:
    """No preferences row: one is created with the regular defaults."""
    NotificationPreference.objects.filter(user=guest_user).delete()

    guest_user.guest = False
    guest_user.save()

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_non_guest_save_is_noop(user: RevelUser) -> None:
    """Saving a regular user does not touch their preferences."""
    prefs = NotificationPreference.objects.get(user=user)
    prefs.notification_type_settings[str(NotificationType.TICKET_CREATED)] = {"enabled": False}
    prefs.save()

    user.first_name = "Changed"
    user.save()

    assert _settings(user)[NotificationType.TICKET_CREATED] == {"enabled": False}


def test_guest_to_guest_save_is_noop(guest_user: RevelUser) -> None:
    """Saving a still-guest user keeps the guest defaults."""
    before = _settings(guest_user)

    guest_user.first_name = "Changed"
    guest_user.save()

    assert _settings(guest_user) == before


def test_false_to_true_flip_does_nothing(user: RevelUser) -> None:
    """Turning a regular user into a guest does not alter preferences."""
    before = _settings(user)

    user.guest = True
    user.save()

    assert _settings(user) == before


def test_reset_is_idempotent(guest_user: RevelUser) -> None:
    """Running the reset twice yields the same result."""
    guest_user.guest = False
    guest_user.save()
    reset_converted_guest_preferences(guest_user)

    assert _settings(guest_user) == get_default_notification_type_settings()
