"""Data migration: backfill ORG_CONTACT_MESSAGE_RECEIVED on existing preferences.

Without this migration, existing users would fall back to their global enabled_channels
(which typically includes email), instead of the intended IN_APP + TELEGRAM only behavior.
The transactional email is sent by a separate task to the org's contact mailbox; we don't
want every staff member to receive a duplicate email by default.
"""

import typing as t

from django.db import migrations


def add_org_contact_message_received_preference(apps: t.Any, schema_editor: t.Any) -> None:
    """Add org_contact_message_received setting to all existing notification preferences.

    - Regular users: enabled=True, channels=["in_app", "telegram"]
    - Guest users: enabled=False
    """
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    notification_type = "org_contact_message_received"
    regular_user_setting = {"enabled": True, "channels": ["in_app", "telegram"]}
    guest_user_setting = {"enabled": False}

    updated_count = 0
    for pref in NotificationPreference.objects.select_related("user").all():
        if notification_type not in pref.notification_type_settings:
            if pref.user.guest:
                pref.notification_type_settings[notification_type] = guest_user_setting
            else:
                pref.notification_type_settings[notification_type] = regular_user_setting
            pref.save(update_fields=["notification_type_settings"])
            updated_count += 1

    if updated_count:
        print(f"  Added {notification_type} preference to {updated_count} users")


def remove_org_contact_message_received_preference(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove org_contact_message_received setting from all notification preferences."""
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")

    notification_type = "org_contact_message_received"

    for pref in NotificationPreference.objects.all():
        if notification_type in pref.notification_type_settings:
            del pref.notification_type_settings[notification_type]
            pref.save(update_fields=["notification_type_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0020_alter_notification_notification_type"),
    ]

    operations = [
        migrations.RunPython(
            add_org_contact_message_received_preference,
            reverse_code=remove_org_contact_message_received_preference,
        ),
    ]
