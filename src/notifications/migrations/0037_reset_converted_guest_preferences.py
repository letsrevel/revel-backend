"""Backfill: give already-converted guests the regular notification defaults (#1248).

Before the conversion fix, turning a guest into a full account left the guest-default overrides in
place. A non-guest row is treated as a leftover guest row when EVERY key of ORIGINAL_GUEST_KEYS is
present and disabled (``enabled`` false, whatever the channels). If one of those is enabled the user
changed their own settings and the row is skipped (conservative). For a matched row every guest-default
key that is present and disabled becomes the regular default (or is removed), independently; absent or
enabled keys are left alone; regular-default keys the row never had are added.

ORIGINAL_GUEST_KEYS are the keys present in the guest map from its refactored form (6d7a1cbf,
2025-11-16) up to today, minus later additions: potluck_item_created_and_claimed (73547ce5, backfilled
ENABLED for converted users by migration 0016), org_contact_message_received (c14cda46, backfilled
ENABLED by 0021), membership_card_updated (1004e0d6, 2026-08-11) and the staff_* types (249788fc, b222fbb0).
Rows created before 6d7a1cbf (migration 0002 era: invitation_claimed, event_created, ...) are not matched.

# ponytail: a regular user who disabled every original guest key would be matched. Vanishingly
# unlikely, and there is no guest history to discriminate.
"""

import typing as t

from django.db import migrations

BATCH_SIZE = 500

# Frozen copies of notifications.models.get_default_notification_type_settings and
# notifications.service.preferences.get_guest_notification_type_settings as of this migration.
REGULAR_DEFAULTS: dict[str, dict[str, t.Any]] = {
    "potluck_item_claimed": {"enabled": True, "channels": ["in_app"]},
    "potluck_item_created": {"enabled": True, "channels": ["in_app"]},
    "potluck_item_created_and_claimed": {"enabled": True, "channels": ["in_app"]},
    "potluck_item_updated": {"enabled": True, "channels": ["in_app"]},
    "potluck_item_unclaimed": {"enabled": True, "channels": ["in_app"]},
    "potluck_item_deleted": {"enabled": True, "channels": ["in_app"]},
    "organization_followed": {"enabled": True, "channels": ["in_app"]},
    "event_series_followed": {"enabled": True, "channels": ["in_app"]},
    "org_contact_message_received": {"enabled": True, "channels": ["in_app", "telegram"]},
}
ORIGINAL_GUEST_KEYS: tuple[str, ...] = (
    "event_open",
    "potluck_item_created",
    "potluck_item_updated",
    "potluck_item_claimed",
    "potluck_item_unclaimed",
    "questionnaire_submitted",
    "invitation_request_created",
    "membership_request_created",
    "membership_granted",
    "membership_promoted",
    "membership_removed",
    "membership_request_approved",
    "membership_request_rejected",
    "org_announcement",
)
# Every guest key as of this migration (original + later additions).
GUEST_DEFAULT_KEYS: tuple[str, ...] = (
    "event_open",
    "potluck_item_created",
    "potluck_item_created_and_claimed",
    "potluck_item_updated",
    "potluck_item_claimed",
    "potluck_item_unclaimed",
    "questionnaire_submitted",
    "invitation_request_created",
    "membership_request_created",
    "membership_granted",
    "membership_promoted",
    "membership_removed",
    "membership_card_updated",
    "membership_request_approved",
    "membership_request_rejected",
    "org_announcement",
    "org_contact_message_received",
    "staff_ticket_created",
    "staff_ticket_cancelled",
    "staff_ticket_refunded",
    "staff_payment_disputed",
    "staff_series_pass_purchased",
    "staff_series_pass_cancelled",
)


def _is_guest_default(value: t.Any) -> bool:
    return isinstance(value, dict) and not value.get("enabled", True)


def _reset(settings: t.Any) -> dict[str, t.Any] | None:
    """Return the reset settings, or None when the row is not a leftover guest row."""
    if not isinstance(settings, dict) or not all(_is_guest_default(settings.get(key)) for key in ORIGINAL_GUEST_KEYS):
        return None
    updated = dict(settings)
    for key in GUEST_DEFAULT_KEYS:
        if not _is_guest_default(updated.get(key)):
            continue
        if key in REGULAR_DEFAULTS:
            updated[key] = {**REGULAR_DEFAULTS[key], "channels": list(REGULAR_DEFAULTS[key]["channels"])}
        else:
            del updated[key]
    for key, value in REGULAR_DEFAULTS.items():
        updated.setdefault(key, {**value, "channels": list(value["channels"])})
    return updated


def reset_converted_guest_preferences(apps: t.Any, schema_editor: t.Any) -> None:
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    candidates = (
        NotificationPreference.objects.filter(user__guest=False, notification_type_settings__has_key=ORIGINAL_GUEST_KEYS[0])
        .only("pk", "notification_type_settings")
        .order_by("pk")
    )
    # pk-sliced batches, not .iterator(): server-side cursors break behind PgBouncer.
    last_pk = None
    while True:
        batch_qs = candidates if last_pk is None else candidates.filter(pk__gt=last_pk)
        batch = list(batch_qs[:BATCH_SIZE])
        if not batch:
            return
        last_pk = batch[-1].pk
        changed = []
        for prefs in batch:
            reset = _reset(prefs.notification_type_settings)
            if reset is not None:
                prefs.notification_type_settings = reset
                changed.append(prefs)
        NotificationPreference.objects.bulk_update(changed, ["notification_type_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0036_alter_notification_notification_type"),
    ]

    operations = [migrations.RunPython(reset_converted_guest_preferences, migrations.RunPython.noop)]
