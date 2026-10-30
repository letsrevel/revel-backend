"""``reset_converted_guest_prefs``: dry-run-by-default legacy backfill for converted guests (#1253)."""

import io
import typing as t

import pytest
from django.core.management import call_command

from accounts.models import RevelUser
from notifications.management.commands.reset_converted_guest_prefs import ORIGINAL_GUEST_KEYS
from notifications.models import NotificationPreference, get_default_notification_type_settings

pytestmark = pytest.mark.django_db


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("reset_converted_guest_prefs", *args, stdout=out)
    return out.getvalue()


def _settings(user: RevelUser) -> dict[str, t.Any]:
    return NotificationPreference.objects.get(user=user).notification_type_settings  # type: ignore[no-any-return]


def _legacy(user: RevelUser, extra: dict[str, t.Any] | None = None) -> None:
    """A realistic pre-fix converted guest: the original keys disabled, then guest=False via bulk write."""
    prefs = user.notification_preferences
    prefs.notification_type_settings = {key: {"enabled": False} for key in ORIGINAL_GUEST_KEYS} | (extra or {})
    prefs.save()
    RevelUser.objects.filter(pk=user.pk).update(guest=False)


def _regular() -> dict[str, t.Any]:
    return {str(k): dict(v) for k, v in get_default_notification_type_settings().items()}


def test_dry_run_writes_nothing_and_reports_counts(guest_user: RevelUser) -> None:
    _legacy(guest_user)
    before = _settings(guest_user)

    out = _run()

    assert _settings(guest_user) == before
    assert "dry-run" in out
    assert "rows matched: 1" in out
    assert "rows changed (would be): 1" in out
    assert "org_announcement: 1" in out


def test_apply_resets_a_matched_legacy_row(guest_user: RevelUser) -> None:
    _legacy(guest_user)

    out = _run("--apply")

    assert "APPLY" in out
    assert _settings(guest_user) == _regular()


def test_other_keys_are_untouched(guest_user: RevelUser) -> None:
    extra = {
        "staff_ticket_created": {"enabled": False},
        "staff_ticket_cancelled": {"enabled": False, "channels": []},
        "membership_card_updated": {"enabled": False},
    }
    _legacy(guest_user, extra)

    _run("--apply")

    settings = _settings(guest_user)
    for key, value in extra.items():
        assert settings[key] == value
    assert "org_announcement" not in settings


def test_enabled_original_key_skips_the_row(guest_user: RevelUser) -> None:
    _legacy(guest_user, {"org_announcement": {"enabled": True}})
    before = _settings(guest_user)

    out = _run("--apply")

    assert _settings(guest_user) == before
    assert "rows matched: 0" in out
    assert "rows skipped (an original key is enabled): 1" in out


def test_regular_user_with_a_few_disabled_types_is_untouched(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    for key in ("event_open", "org_announcement"):
        prefs.notification_type_settings[key] = {"enabled": False}
    prefs.save()
    before = _settings(regular_user)

    _run("--apply")

    assert _settings(regular_user) == before


def test_still_a_guest_is_untouched(guest_user: RevelUser) -> None:
    before = _settings(guest_user)

    out = _run("--apply")

    assert _settings(guest_user) == before
    assert "rows scanned: 0" in out


def test_pinned_defaults_are_only_reported_without_the_flag(guest_user: RevelUser) -> None:
    pinned = {
        "org_contact_message_received": {"enabled": False},
        "potluck_item_created_and_claimed": {"enabled": False},
    }
    _legacy(guest_user, pinned)

    out = _run("--apply")

    assert "pinned-key candidates (exactly {'enabled': False}): 2" in out
    settings = _settings(guest_user)
    assert settings["org_contact_message_received"] == {"enabled": False}
    assert settings["potluck_item_created_and_claimed"] == {"enabled": False}


def test_include_pinned_defaults_resets_exact_guest_shape_only(guest_user: RevelUser) -> None:
    _legacy(
        guest_user,
        {
            "org_contact_message_received": {"enabled": False},
            "potluck_item_created_and_claimed": {"enabled": False, "channels": []},
        },
    )

    _run("--apply", "--include-pinned-defaults")

    settings = _settings(guest_user)
    assert settings["org_contact_message_received"] == _regular()["org_contact_message_received"]
    assert settings["potluck_item_created_and_claimed"] == {"enabled": False, "channels": []}  # not the exact shape


def test_second_apply_is_idempotent(guest_user: RevelUser) -> None:
    _legacy(guest_user)
    _run("--apply")
    after_first = _settings(guest_user)

    out = _run("--apply")

    assert _settings(guest_user) == after_first == _regular()
    assert "rows matched: 0" in out


def test_output_contains_no_email_addresses(guest_user: RevelUser) -> None:
    _legacy(guest_user)

    out = _run("--show-ids", "5")

    assert "@" not in out
    assert str(guest_user.pk) in out
