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


def test_explicit_value_on_converted_guest_is_kept(guest_user: RevelUser) -> None:
    explicit = {"enabled": True, "channels": ["email"]}
    prefs = guest_user.notification_preferences
    prefs.notification_type_settings["org_announcement"] = explicit
    prefs.save()
    _convert_without_hook(guest_user)

    _run()

    assert _settings(guest_user)["org_announcement"] == explicit


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
