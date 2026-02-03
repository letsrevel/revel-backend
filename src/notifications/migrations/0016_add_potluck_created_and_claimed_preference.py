"""Data migration to add potluck_item_created_and_claimed to existing notification preferences.

Without this migration, existing users would fall back to their global enabled_channels
(which typically includes email), instead of the intended IN_APP only behavior.
"""

import typing as t

from django.db import migrations


def add_potluck_created_and_claimed_preference(apps: t.Any, schema_editor: t.Any) -> None:
    """Add potluck_item_created_and_claimed setting to all existing notification preferences.

    - Regular users: enabled=True, channels=["in_app"] (matching get_default_notification_type_settings)
    - Guest users: enabled=False (matching _get_guest_notification_type_settings)
    """
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    notification_type = "potluck_item_created_and_claimed"
    regular_user_setting = {"enabled": True, "channels": ["in_app"]}
    guest_user_setting = {"enabled": False}

    updated_count = 0
    for pref in NotificationPreference.objects.select_related("user").all():
        # Only add if not already present (in case migration is re-run)
        if notification_type not in pref.notification_type_settings:
            if pref.user.guest:
                pref.notification_type_settings[notification_type] = guest_user_setting
            else:
                pref.notification_type_settings[notification_type] = regular_user_setting
            pref.save(update_fields=["notification_type_settings"])
            updated_count += 1

    if updated_count:
        print(f"  Added {notification_type} preference to {updated_count} users")


def remove_potluck_created_and_claimed_preference(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove potluck_item_created_and_claimed setting from all notification preferences."""
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    notification_type = "potluck_item_created_and_claimed"

    for pref in NotificationPreference.objects.all():
        if notification_type in pref.notification_type_settings:
            del pref.notification_type_settings[notification_type]
            pref.save(update_fields=["notification_type_settings"])


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0015_alter_notification_notification_type"),
    ]

    operations = [
        migrations.RunPython(
            add_potluck_created_and_claimed_preference,
            reverse_code=remove_potluck_created_and_claimed_preference,
        ),
    ]
