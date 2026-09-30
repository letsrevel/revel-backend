"""Undo the per-type channel pins written by the pre-#1030 ``confirm_unsubscribe``.

That function copied the submitted ``enabled_channels`` into a per-type override for
EVERY notification type. Per-type channels override the global list, so a later global
re-enable did nothing, and types with their own defaults (potluck → in-app only,
org contact → in-app + Telegram) were re-routed.

A row is treated as pinned when it has an entry for ``ticket_created`` (absent from both
default maps, so only the old sync wrote it) and every entry carries an identical
``channels`` list. Those rows get the default map back (guest map for guests), keeping
``enabled: False`` wherever the user had disabled a type. Rows a user customised
afterwards (channels differ between types) are left alone. Global fields are untouched.
"""

import typing as t

from django.db import migrations

BATCH_SIZE = 500

# Frozen copies of notifications.models.get_default_notification_type_settings and
# notifications.signals.user._get_guest_notification_type_settings as of this migration.
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
GUEST_DEFAULTS: dict[str, dict[str, t.Any]] = {
    notification_type: {"enabled": False}
    for notification_type in (
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
    )
}
MARKER_TYPE = "ticket_created"


def _is_pinned(settings: t.Any) -> bool:
    if not isinstance(settings, dict) or MARKER_TYPE not in settings:
        return False
    channel_lists = [entry.get("channels") if isinstance(entry, dict) else None for entry in settings.values()]
    first = channel_lists[0]
    return isinstance(first, list) and all(channels == first for channels in channel_lists)


def _restored(settings: dict[str, t.Any], is_guest: bool) -> dict[str, t.Any]:
    defaults = GUEST_DEFAULTS if is_guest else REGULAR_DEFAULTS
    restored = {notification_type: dict(entry) for notification_type, entry in defaults.items()}
    for notification_type, entry in settings.items():
        if entry.get("enabled") is False:
            restored[notification_type] = {**restored.get(notification_type, {}), "enabled": False}
    return restored


def unpin_unsubscribe_overrides(apps: t.Any, schema_editor: t.Any) -> None:
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    candidates = (
        NotificationPreference.objects.filter(notification_type_settings__has_key=MARKER_TYPE)
        .select_related("user")
        .only("pk", "notification_type_settings", "user__guest")
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
            if _is_pinned(prefs.notification_type_settings):
                prefs.notification_type_settings = _restored(prefs.notification_type_settings, prefs.user.guest)
                changed.append(prefs)
        NotificationPreference.objects.bulk_update(changed, ["notification_type_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0027_email_posture"),
    ]

    operations = [migrations.RunPython(unpin_unsubscribe_overrides, migrations.RunPython.noop)]
