"""Data migration to create OrganizationFollow records for existing members.

This migration automatically creates follow relationships for all existing
active organization members, so they will receive notifications about new
events from organizations they're already members of.
"""

from django.db import migrations


def create_follows_for_existing_members(apps, schema_editor):
    """Create OrganizationFollow for each active OrganizationMember."""
    OrganizationMember = apps.get_model("events", "OrganizationMember")
    OrganizationFollow = apps.get_model("events", "OrganizationFollow")

    # Get all active members (not cancelled or banned)
    active_members = OrganizationMember.objects.filter(
        status__in=["active", "paused"]
    ).select_related("user", "organization")

    follows_to_create = []
    for member in active_members:
        # Check if follow already exists (shouldn't, but be safe)
        if not OrganizationFollow.objects.filter(
            user=member.user,
            organization=member.organization,
        ).exists():
            follows_to_create.append(
                OrganizationFollow(
                    user=member.user,
                    organization=member.organization,
                    notify_new_events=True,
                    notify_announcements=True,
                    is_public=False,
                    is_archived=False,
                )
            )

    if follows_to_create:
        OrganizationFollow.objects.bulk_create(follows_to_create, ignore_conflicts=True)
        print(f"Created {len(follows_to_create)} OrganizationFollow records for existing members")


def reverse_migration(apps, schema_editor):
    """Remove OrganizationFollow records created by this migration.

    Note: This is a rough reversal - it removes ALL follows, not just those
    created by this migration. In practice, this migration should not be reversed.
    """
    OrganizationFollow = apps.get_model("events", "OrganizationFollow")
    # We can't easily identify which follows were created by this migration,
    # so we'll leave them in place on reverse. This is a one-way migration.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0037_add_organization_and_series_follow_models"),
    ]

    operations = [
        migrations.RunPython(
            create_follows_for_existing_members,
            reverse_migration,
        ),
    ]
