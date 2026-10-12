"""Copy per-type overrides of the buyer receipts to the new staff alert types (#1127).

Staff alerts used to ride the buyer types (``ticket_created`` ...) and followed the staff
member's overrides for them. They now have their own types, so a user who had unticked e.g.
``ticket_created`` keeps that choice for ``staff_ticket_created``. The buyer key is left
untouched (it still governs nothing for non-mandatory ones, and is the receipt switch for the rest).
Reverse removes the new keys.
"""

import typing as t

from django.db import migrations

BATCH_SIZE = 500

# buyer type -> staff alert type
STAFF_TYPE_FOR: dict[str, str] = {
    "ticket_created": "staff_ticket_created",
    "ticket_cancelled": "staff_ticket_cancelled",
    "ticket_refunded": "staff_ticket_refunded",
    "series_pass_purchased": "staff_series_pass_purchased",
    "series_pass_cancelled": "staff_series_pass_cancelled",
}


def copy_overrides_to_staff_types(apps: t.Any, schema_editor: t.Any) -> None:
    """Copy each existing buyer-type override to its staff type when the latter is absent."""
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    # Materialise the ids (no server-side cursor across the writes below — PgBouncer-safe).
    pref_ids = list(NotificationPreference.objects.values_list("id", flat=True))
    for start in range(0, len(pref_ids), BATCH_SIZE):
        changed = []
        for pref in NotificationPreference.objects.filter(id__in=pref_ids[start : start + BATCH_SIZE]):
            settings = pref.notification_type_settings
            dirty = False
            for buyer_type, staff_type in STAFF_TYPE_FOR.items():
                if buyer_type in settings and staff_type not in settings:
                    settings[staff_type] = dict(settings[buyer_type])
                    dirty = True
            if dirty:
                changed.append(pref)
        NotificationPreference.objects.bulk_update(changed, ["notification_type_settings"])


def remove_staff_type_overrides(apps: t.Any, schema_editor: t.Any) -> None:
    """Drop the staff type keys from every preference."""
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    pref_ids = list(NotificationPreference.objects.values_list("id", flat=True))
    for start in range(0, len(pref_ids), BATCH_SIZE):
        changed = []
        for pref in NotificationPreference.objects.filter(id__in=pref_ids[start : start + BATCH_SIZE]):
            removed = [k for k in STAFF_TYPE_FOR.values() if pref.notification_type_settings.pop(k, None) is not None]
            if removed:
                changed.append(pref)
        NotificationPreference.objects.bulk_update(changed, ["notification_type_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0032_staff_alert_types"),
    ]

    operations = [
        migrations.RunPython(copy_overrides_to_staff_types, remove_staff_type_overrides),
    ]
