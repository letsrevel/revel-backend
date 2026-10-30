"""Data migration 0037: reset leftover guest-default notification preferences of converted guests (#1248)."""

import typing as t
from importlib import import_module

import pytest
from django.apps import apps

from accounts.models import RevelUser
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.service.preferences import get_guest_notification_type_settings

pytestmark = pytest.mark.django_db

migration = import_module("notifications.migrations.0037_reset_converted_guest_preferences")


def _run() -> None:
    migration.reset_converted_guest_preferences(apps, None)


def _settings(user: RevelUser) -> dict[str, t.Any]:
    return NotificationPreference.objects.get(user=user).notification_type_settings  # type: ignore[no-any-return]


def _convert_without_hook(user: RevelUser) -> None:
    """Simulate a pre-fix conversion: the flag flips, the guest-default overrides stay."""
    RevelUser.objects.filter(pk=user.pk).update(guest=False)


def test_frozen_copies_match_current_defaults() -> None:
    """The migration's inline copies must equal the live defaults at the time it was written."""
    assert migration.REGULAR_DEFAULTS == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}
    assert set(migration.GUEST_DEFAULT_KEYS) == {str(k) for k in get_guest_notification_type_settings()}


def test_converted_guest_row_is_reset(guest_user: RevelUser) -> None:
    _convert_without_hook(guest_user)

    _run()

    assert _settings(guest_user) == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_converted_guest_with_empty_channels_shape_is_reset(guest_user: RevelUser) -> None:
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings = {key: {"enabled": False, "channels": []} for key in migration.GUEST_DEFAULT_KEYS}
    prefs.save()
    _convert_without_hook(guest_user)

    _run()

    assert _settings(guest_user) == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_converted_guest_with_legacy_pinned_channels_is_reset(guest_user: RevelUser) -> None:
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings = {
        key: {"enabled": False, "channels": ["in_app", "email"]} for key in migration.GUEST_DEFAULT_KEYS
    }
    prefs.save()
    _convert_without_hook(guest_user)

    _run()

    assert _settings(guest_user) == {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_explicit_value_on_converted_guest_is_kept(guest_user: RevelUser) -> None:
    explicit = {"enabled": True, "channels": ["email"]}
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings["membership_card_updated"] = explicit  # not an original key
    prefs.save()
    _convert_without_hook(guest_user)

    _run()

    settings = _settings(guest_user)
    assert settings["membership_card_updated"] == explicit
    assert "org_announcement" not in settings  # the rest WAS reset
    assert settings["org_contact_message_received"] == migration.REGULAR_DEFAULTS["org_contact_message_received"]


def test_regular_row_is_untouched(regular_user: RevelUser) -> None:
    before = _settings(regular_user)

    _run()

    assert _settings(regular_user) == before


def test_regular_row_with_a_few_disabled_types_is_untouched(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.notification_type_settings["org_announcement"] = {"enabled": False}
    prefs.save()
    before = _settings(regular_user)

    _run()

    assert _settings(regular_user) == before


def test_guest_row_is_untouched(guest_user: RevelUser) -> None:
    before = _settings(guest_user)

    _run()

    assert _settings(guest_user) == before


def _legacy_row(user: RevelUser, keys: t.Iterable[str]) -> None:
    prefs = user.notification_preferences
    prefs.notification_type_settings = {key: {"enabled": False} for key in keys}
    prefs.save()
    _convert_without_hook(user)


def _regular_defaults() -> dict[str, t.Any]:
    return {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_original_keys_are_a_subset_of_current_guest_keys() -> None:
    assert len(migration.ORIGINAL_GUEST_KEYS) == 14
    later = {"potluck_item_created_and_claimed", "org_contact_message_received", "membership_card_updated"}
    assert set(migration.ORIGINAL_GUEST_KEYS) < set(migration.GUEST_DEFAULT_KEYS)
    assert not later & set(migration.ORIGINAL_GUEST_KEYS)
    assert not any(k.startswith("staff_") for k in migration.ORIGINAL_GUEST_KEYS)


def test_pre_878_row_without_membership_card_updated_is_reset(guest_user: RevelUser) -> None:
    """Realistic prod row: original keys only, no membership_card_updated, no staff_*."""
    _legacy_row(guest_user, migration.ORIGINAL_GUEST_KEYS)

    _run()

    assert _settings(guest_user) == _regular_defaults()


def test_row_with_backfilled_enabled_keys_is_reset_and_keeps_them(guest_user: RevelUser) -> None:
    """Migrations 0016/0021 wrote these two as ENABLED for already-converted users."""
    _legacy_row(guest_user, migration.ORIGINAL_GUEST_KEYS)
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings["potluck_item_created_and_claimed"] = {"enabled": True, "channels": ["in_app"]}
    prefs.notification_type_settings["org_contact_message_received"] = {
        "enabled": True,
        "channels": ["in_app", "telegram"],
    }
    prefs.save()

    _run()

    settings = _settings(guest_user)
    assert settings == _regular_defaults()
    assert "org_announcement" not in settings


def test_row_with_an_original_key_enabled_is_skipped(guest_user: RevelUser) -> None:
    """An explicitly enabled original key is a deliberate user choice: leave the whole row alone."""
    _legacy_row(guest_user, migration.ORIGINAL_GUEST_KEYS)
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings["org_announcement"] = {"enabled": True}
    prefs.save()
    before = _settings(guest_user)

    _run()

    assert _settings(guest_user) == before


def test_regular_user_with_a_few_disabled_types_is_untouched(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    for key in ("event_open", "org_announcement", "membership_granted"):
        prefs.notification_type_settings[key] = {"enabled": False}
    prefs.save()
    before = _settings(regular_user)

    _run()

    assert _settings(regular_user) == before


def test_rerun_is_idempotent(guest_user: RevelUser) -> None:
    _legacy_row(guest_user, migration.ORIGINAL_GUEST_KEYS)
    _run()
    after_first = _settings(guest_user)

    _run()

    assert _settings(guest_user) == after_first == _regular_defaults()
