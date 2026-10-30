"""Backfill: give already-converted guests the regular notification defaults (#1248).

Before the guest-transition hook, converting a guest to a full account left the guest-default
overrides (every guest-default type disabled) in place. A non-guest row is treated as affected when
EVERY guest-default key is present and disabled (``enabled`` false, whatever its channels). Those
rows get the same reset as ``notifications.service.preferences.reset_converted_guest_preferences``:
guest-default keys that are still disabled become the regular default (or are
removed), and regular-default keys the row never had are added. Everything else is untouched.
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
    if not isinstance(settings, dict) or not all(_is_guest_default(settings.get(key)) for key in GUEST_DEFAULT_KEYS):
        return None
    updated = dict(settings)
    for key in GUEST_DEFAULT_KEYS:
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
        NotificationPreference.objects.filter(user__guest=False, notification_type_settings__has_key=GUEST_DEFAULT_KEYS[0])
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
