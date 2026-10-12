"""Data migrations 0033/0034: staff alert preferences copy and history rewrite (#1127)."""

import typing as t
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection

from accounts.models import RevelUser
from notifications.models import Notification, NotificationPreference

pytestmark = pytest.mark.django_db

prefs_migration = import_module("notifications.migrations.0033_copy_staff_alert_preferences")
history_migration = import_module("notifications.migrations.0034_rewrite_staff_alert_history")

PAIRS = [
    ("ticket_created", "staff_ticket_created"),
    ("ticket_cancelled", "staff_ticket_cancelled"),
    ("ticket_refunded", "staff_ticket_refunded"),
    ("series_pass_purchased", "staff_series_pass_purchased"),
    ("series_pass_cancelled", "staff_series_pass_cancelled"),
]


def _settings(user: RevelUser) -> dict[str, t.Any]:
    return NotificationPreference.objects.get(user=user).notification_type_settings  # type: ignore[no-any-return]


def _set_settings(user: RevelUser, settings: dict[str, t.Any]) -> None:
    prefs = user.notification_preferences
    prefs.notification_type_settings = settings
    prefs.save()


def _make_notification(user: RevelUser, notification_type: str, context: dict[str, t.Any]) -> Notification:
    return Notification.objects.create(
        user=user, notification_type=notification_type, context=context, title="t", body="b"
    )


def _run_history(sql: list[str]) -> None:
    with connection.cursor() as cursor:
        for statement in sql:
            cursor.execute(statement)


# ---- 0033: preference copy ----


def test_prefs_migration_pairs_match_the_enum() -> None:
    assert list(prefs_migration.STAFF_TYPE_FOR.items()) == PAIRS


def test_overrides_are_copied_to_staff_types(regular_user: RevelUser) -> None:
    _set_settings(
        regular_user,
        {
            "ticket_created": {"enabled": False},
            "ticket_refunded": {"enabled": True, "channels": ["in_app"]},
            "series_pass_cancelled": {"enabled": True, "channels": ["email"]},
        },
    )

    prefs_migration.copy_overrides_to_staff_types(apps, None)

    assert _settings(regular_user) == {
        "ticket_created": {"enabled": False},
        "staff_ticket_created": {"enabled": False},
        "ticket_refunded": {"enabled": True, "channels": ["in_app"]},
        "staff_ticket_refunded": {"enabled": True, "channels": ["in_app"]},
        "series_pass_cancelled": {"enabled": True, "channels": ["email"]},
        "staff_series_pass_cancelled": {"enabled": True, "channels": ["email"]},
    }


def test_existing_staff_override_is_not_overwritten(regular_user: RevelUser) -> None:
    _set_settings(
        regular_user,
        {"ticket_created": {"enabled": False}, "staff_ticket_created": {"enabled": True}},
    )

    prefs_migration.copy_overrides_to_staff_types(apps, None)

    assert _settings(regular_user)["staff_ticket_created"] == {"enabled": True}


def test_users_without_buyer_overrides_are_untouched(regular_user: RevelUser) -> None:
    before = _settings(regular_user)

    prefs_migration.copy_overrides_to_staff_types(apps, None)

    assert _settings(regular_user) == before


def test_copy_is_idempotent_and_reversible(regular_user: RevelUser) -> None:
    _set_settings(regular_user, {"ticket_created": {"enabled": False}})

    prefs_migration.copy_overrides_to_staff_types(apps, None)
    prefs_migration.copy_overrides_to_staff_types(apps, None)
    assert "staff_ticket_created" in _settings(regular_user)

    prefs_migration.remove_staff_type_overrides(apps, None)
    assert _settings(regular_user) == {"ticket_created": {"enabled": False}}


# ---- 0034: history rewrite ----


@pytest.mark.parametrize(("buyer", "staff"), PAIRS)
def test_flagged_rows_are_retyped_and_unflagged(regular_user: RevelUser, buyer: str, staff: str) -> None:
    flagged = _make_notification(regular_user, buyer, {"k": "v", "staff_alert": True})
    receipt = _make_notification(regular_user, buyer, {"k": "v"})

    _run_history(history_migration.FORWARD)

    flagged.refresh_from_db()
    receipt.refresh_from_db()
    assert flagged.notification_type == staff
    assert flagged.context == {"k": "v"}
    assert receipt.notification_type == buyer
    assert receipt.context == {"k": "v"}


def test_flag_is_stripped_from_unmapped_types_too(regular_user: RevelUser) -> None:
    row = _make_notification(regular_user, "membership_request_created", {"k": "v", "staff_alert": True})

    _run_history(history_migration.FORWARD)

    row.refresh_from_db()
    assert row.notification_type == "membership_request_created"
    assert row.context == {"k": "v"}


@pytest.mark.parametrize(("buyer", "staff"), PAIRS)
def test_reverse_maps_back_and_restores_the_flag(regular_user: RevelUser, buyer: str, staff: str) -> None:
    staff_row = _make_notification(regular_user, staff, {"k": "v"})
    receipt = _make_notification(regular_user, buyer, {"k": "v"})

    _run_history(history_migration.REVERSE)

    staff_row.refresh_from_db()
    receipt.refresh_from_db()
    assert staff_row.notification_type == buyer
    assert staff_row.context == {"k": "v", "staff_alert": True}
    assert receipt.context == {"k": "v"}
