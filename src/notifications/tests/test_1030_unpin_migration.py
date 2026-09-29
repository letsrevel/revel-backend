"""Data migration 0028: undo the per-type channel pins written by the old confirm_unsubscribe (#1030)."""

import typing as t
from importlib import import_module

import pytest
from django.apps import apps

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.signals.user import _get_guest_notification_type_settings

pytestmark = pytest.mark.django_db

migration = import_module("notifications.migrations.0028_unpin_unsubscribe_overrides")


def _legacy_unsubscribe_pin(prefs: NotificationPreference, channels: list[str]) -> None:
    """What confirm_unsubscribe wrote before #1030: the submitted channels on every type."""
    existing = prefs.notification_type_settings
    prefs.notification_type_settings = {
        nt.value: {"enabled": existing.get(nt.value, {}).get("enabled", True), "channels": channels}
        for nt in NotificationType
    }
    prefs.enabled_channels = channels
    prefs.silence_all_notifications = True
    prefs.save()


def _run() -> None:
    migration.unpin_unsubscribe_overrides(apps, None)


def _settings(user: RevelUser) -> dict[str, t.Any]:
    return NotificationPreference.objects.get(user=user).notification_type_settings  # type: ignore[no-any-return]


def test_frozen_defaults_match_current_defaults() -> None:
    """The migration's inline copies must equal the defaults at the time it was written."""
    assert migration.REGULAR_DEFAULTS == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}
    assert migration.GUEST_DEFAULTS == {str(k): v for k, v in _get_guest_notification_type_settings().items()}


def test_regular_user_pin_restored_to_defaults(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.notification_type_settings[NotificationType.EVENT_UPDATED] = {"enabled": False}
    prefs.notification_type_settings[NotificationType.POTLUCK_ITEM_CREATED] = {
        "enabled": False,
        "channels": [DeliveryChannel.IN_APP],
    }
    prefs.save()
    _legacy_unsubscribe_pin(prefs, [DeliveryChannel.IN_APP])

    _run()

    expected = {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}
    expected[NotificationType.EVENT_UPDATED] = {"enabled": False}
    expected[NotificationType.POTLUCK_ITEM_CREATED] = {"enabled": False, "channels": [DeliveryChannel.IN_APP]}
    assert _settings(regular_user) == expected
    prefs.refresh_from_db()
    # Global choices are the user's and stay as submitted.
    assert prefs.enabled_channels == [DeliveryChannel.IN_APP]
    assert prefs.silence_all_notifications is True


def test_guest_pin_restored_to_guest_defaults(guest_user: RevelUser) -> None:
    _legacy_unsubscribe_pin(guest_user.notification_preferences, [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL])

    _run()

    assert _settings(guest_user) == {str(k): v for k, v in _get_guest_notification_type_settings().items()}


def test_empty_channel_pin_restored(regular_user: RevelUser) -> None:
    _legacy_unsubscribe_pin(regular_user.notification_preferences, [])

    _run()

    assert _settings(regular_user) == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_user_customised_one_type_is_skipped(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    _legacy_unsubscribe_pin(prefs, [DeliveryChannel.IN_APP])
    prefs.notification_type_settings[NotificationType.EVENT_REMINDER]["channels"] = [DeliveryChannel.EMAIL]
    prefs.save()
    before = _settings(regular_user)

    _run()

    assert _settings(regular_user) == before


def test_entry_without_channels_is_skipped(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    _legacy_unsubscribe_pin(prefs, [DeliveryChannel.IN_APP])
    prefs.notification_type_settings[NotificationType.EVENT_REMINDER] = {"enabled": True}
    prefs.save()
    before = _settings(regular_user)

    _run()

    assert _settings(regular_user) == before


def test_untouched_preferences_are_skipped(regular_user: RevelUser, guest_user: RevelUser) -> None:
    before = {u.pk: _settings(u) for u in (regular_user, guest_user)}

    _run()

    assert {u.pk: _settings(u) for u in (regular_user, guest_user)} == before


def test_batches_and_is_idempotent(django_user_model: type[RevelUser], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration, "BATCH_SIZE", 2)
    users = [
        django_user_model.objects.create_user(username=f"u{i}@example.com", email=f"u{i}@example.com", password="pw")
        for i in range(5)
    ]
    for user in users:
        _legacy_unsubscribe_pin(user.notification_preferences, [DeliveryChannel.IN_APP])

    _run()
    _run()

    expected = {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}
    assert all(_settings(user) == expected for user in users)
