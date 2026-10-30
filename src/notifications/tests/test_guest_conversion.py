"""Guest -> account conversion resets notification preferences to regular defaults (#1248)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.service.preferences import reset_converted_guest_preferences

pytestmark = pytest.mark.django_db

CaptureOnCommit = t.Any  # the pytest-django ``django_capture_on_commit_callbacks`` fixture


def _settings(user: RevelUser) -> dict[str, object]:
    return dict(NotificationPreference.objects.get(user=user).notification_type_settings)


def _convert(user: RevelUser, capture: CaptureOnCommit, **save_kwargs: t.Any) -> None:
    """Flip guest -> False and save, running the on_commit reset."""
    user.guest = False
    with capture(execute=True):
        user.save(**save_kwargs)


def test_plain_conversion_gives_regular_defaults(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The admin path: untick guest and save."""
    _convert(guest_user, django_capture_on_commit_callbacks)

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_conversion_with_guest_in_update_fields(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A partial save that includes the guest flag still triggers the reset."""
    _convert(guest_user, django_capture_on_commit_callbacks, update_fields=["guest"])

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_partial_save_without_guest_costs_no_extra_query(user: RevelUser, django_assert_num_queries: t.Any) -> None:
    """A save that can't touch the guest flag (e.g. last_login on login) costs no extra query."""
    with django_assert_num_queries(1):
        user.save(update_fields=["last_login"])


def test_reset_is_deferred_to_commit(guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """Nothing runs inside the saving transaction; the callback does the work."""
    guest_user.guest = False
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        guest_user.save()

    assert len(callbacks) == 1
    assert _settings(guest_user) != get_default_notification_type_settings()


def test_reset_failure_does_not_break_the_save(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A preference failure is swallowed by the robust on_commit and never raised to the caller."""
    guest_user.guest = False
    with patch("notifications.signals.user.reset_converted_guest_preferences", side_effect=RuntimeError("boom")):
        with django_capture_on_commit_callbacks(execute=True):
            guest_user.save()

    guest_user.refresh_from_db()
    assert guest_user.guest is False


def test_second_save_after_conversion_does_not_rerun(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The loaded flag is cleared once the transition fired."""
    _convert(guest_user, django_capture_on_commit_callbacks)

    guest_user.first_name = "Changed"
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        guest_user.save()

    assert callbacks == []


def test_explicit_values_are_preserved(guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """A non-guest-default value is a user choice and survives; the rest is reset."""
    explicit = {"enabled": True, "channels": ["email"]}
    prefs = NotificationPreference.objects.get(user=guest_user)
    prefs.notification_type_settings[str(NotificationType.ORG_ANNOUNCEMENT)] = explicit
    prefs.save()

    _convert(guest_user, django_capture_on_commit_callbacks)

    expected = {**get_default_notification_type_settings(), NotificationType.ORG_ANNOUNCEMENT: explicit}
    assert _settings(guest_user) == expected


def test_disabled_with_empty_channels_counts_as_guest_default(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """``{"enabled": False, "channels": []}`` (API / unsubscribe shape) is reset like the bare default."""
    prefs = NotificationPreference.objects.get(user=guest_user)
    prefs.notification_type_settings[str(NotificationType.ORG_ANNOUNCEMENT)] = {"enabled": False, "channels": []}
    prefs.notification_type_settings[str(NotificationType.MEMBERSHIP_GRANTED)] = {"enabled": False, "channels": []}
    prefs.save()

    _convert(guest_user, django_capture_on_commit_callbacks)

    settings = _settings(guest_user)
    assert NotificationType.ORG_ANNOUNCEMENT not in settings
    assert NotificationType.MEMBERSHIP_GRANTED not in settings
    assert settings == get_default_notification_type_settings()


def test_pinned_defaults_are_restored_not_dropped(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Keys with a pinned regular default get it back (not the in_app+email fallback)."""
    _convert(guest_user, django_capture_on_commit_callbacks)

    settings = _settings(guest_user)
    assert settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED] == {
        "enabled": True,
        "channels": ["in_app", "telegram"],
    }
    assert NotificationType.ORGANIZATION_FOLLOWED in settings
    assert NotificationType.MEMBERSHIP_GRANTED not in settings


def test_missing_row_is_created_with_regular_defaults(
    guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """No preferences row: one is created with the regular defaults."""
    NotificationPreference.objects.filter(user=guest_user).delete()

    _convert(guest_user, django_capture_on_commit_callbacks)

    assert _settings(guest_user) == get_default_notification_type_settings()


def test_non_guest_save_is_noop(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """Saving a regular user does not touch their preferences."""
    prefs = NotificationPreference.objects.get(user=user)
    prefs.notification_type_settings[str(NotificationType.TICKET_CREATED)] = {"enabled": False}
    prefs.save()

    user.first_name = "Changed"
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        user.save()

    assert callbacks == []
    assert _settings(user)[NotificationType.TICKET_CREATED] == {"enabled": False}


def test_guest_to_guest_save_is_noop(guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """Saving a still-guest user keeps the guest defaults."""
    before = _settings(guest_user)

    guest_user.first_name = "Changed"
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        guest_user.save()

    assert callbacks == []
    assert _settings(guest_user) == before


def test_false_to_true_flip_does_nothing(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """Turning a regular user into a guest does not alter preferences."""
    before = _settings(user)

    user.guest = True
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        user.save()

    assert callbacks == []
    assert _settings(user) == before


def test_reset_returns_changed_flag(guest_user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """True when something changed, False on the idempotent second run."""
    _convert(guest_user, django_capture_on_commit_callbacks)

    assert reset_converted_guest_preferences(guest_user) is False
    assert _settings(guest_user) == get_default_notification_type_settings()
