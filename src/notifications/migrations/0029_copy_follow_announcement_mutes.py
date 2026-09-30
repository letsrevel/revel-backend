"""Copy legacy follow-level announcement opt-outs into the per-org mute (#1031).

``OrganizationFollow.notify_announcements`` was never read; ``NotificationPreference.muted_organizations``
is now the source of truth. The column is kept (dropped in a follow-up) so this is safe to roll back;
the reverse is a no-op because mutes added afterwards can't be told apart from copied ones.
"""

import typing as t

from django.db import migrations

BATCH_SIZE = 1000


def copy_follow_announcement_mutes(apps: t.Any, schema_editor: t.Any) -> None:
    """Mute the org for every active follow with ``notify_announcements=False`` (idempotent)."""
    OrganizationFollow = apps.get_model("events", "OrganizationFollow")
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    Mute = NotificationPreference.muted_organizations.through

    # ponytail: loads all opt-out pairs at once — tiny in practice (the toggle did nothing);
    # page by pk if it ever isn't.
    pairs = list(
        OrganizationFollow.objects.filter(notify_announcements=False, is_archived=False).values_list(
            "user_id", "organization_id"
        )
    )
    if not pairs:
        return

    user_ids = {user_id for user_id, _ in pairs}
    # Preferences are created by a post_save signal; backfill any row that is somehow missing.
    existing = set(NotificationPreference.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True))
    NotificationPreference.objects.bulk_create(
        [NotificationPreference(user_id=user_id) for user_id in user_ids - existing],
        ignore_conflicts=True,
    )
    prefs_by_user = dict(NotificationPreference.objects.filter(user_id__in=user_ids).values_list("user_id", "id"))
    Mute.objects.bulk_create(
        [
            Mute(notificationpreference_id=prefs_by_user[user_id], organization_id=organization_id)
            for user_id, organization_id in pairs
        ],
        ignore_conflicts=True,
        batch_size=BATCH_SIZE,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0122_ticket_tier_check_in_offsets"),
        ("notifications", "0028_unpin_unsubscribe_overrides"),
    ]

    operations = [migrations.RunPython(copy_follow_announcement_mutes, migrations.RunPython.noop)]
